from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.pool import NullPool

from backend import agent_service, main
from backend.main import app
from backend.seed import seed_database
from backend.store import (
    MAX_OVERFLOW,
    MAX_POOL_SIZE,
    SCHEMA,
    STALE_WORK_REASON,
    Store,
    runs,
    sqlalchemy_postgres_url,
)

RECOMMENDATION = {
    "outcome": "approve",
    "rationale": "Eligible under the sample policy.",
    "policy_citations": ["Cancellation Policy: 2. Customer-requested cancellations"],
    "next_action": "Support agent to review.",
    "priority_human_review": False,
}


TEST_API_KEY = "test-agent-secret-key-0123456789abcdef"
AGENT_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "runs.sqlite3")
    result.initialize()
    return result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_API_KEY)

    class AcceptedResponse:
        status_code = 200
        text = '{"accepted": true}'

        def json(self) -> dict[str, bool]:
            return {"accepted": True}

    monkeypatch.setattr(
        agent_service.httpx,
        "post",
        lambda url, **kwargs: AcceptedResponse(),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_first_failure_returns_a_request_to_the_queue(store):
    assert store.start_request("REQ-7001") == 1
    assert store.fail_request("REQ-7001") == "waiting"
    request = store.get_request("REQ-7001")
    assert request is not None
    assert request["status"] == "waiting"
    assert request["attempts"] == 1
    assert request["steps"][-1]["stage"] == "error"


def test_second_failure_leaves_a_request_for_support(store):
    store.start_request("REQ-7001")
    store.fail_request("REQ-7001")
    assert store.start_request("REQ-7001") == 2
    assert store.fail_request("REQ-7001") == "left_for_support"
    request = store.get_request("REQ-7001")
    assert request is not None
    assert request["status"] == "waiting_for_agent_review"
    assert request["recommendation"] is None
    stages = [step["stage"] for step in request["steps"]]
    assert stages == ["started", "error", "started", "left_for_support"]
    assert "Attempt 2 of 2" in request["steps"][2]["message"]
    assert store.start_request("REQ-7001") is None


def test_a_request_never_gets_a_second_recommendation(store):
    store.start_request("REQ-7001")
    assert store.save_recommendation("REQ-7001", RECOMMENDATION)
    assert store.save_recommendation("REQ-7001", RECOMMENDATION)
    assert not store.save_recommendation("REQ-7001", {**RECOMMENDATION, "x": 1})
    assert store.start_request("REQ-7001") is None
    review = {"outcome": "approve", "notes": "", "reviewed_by": "Priya Nair"}
    assert store.record_review("REQ-7001", review) == "decided"
    assert store.record_review("REQ-7001", review) == "decided"
    assert (
        store.record_review(
            "REQ-7001",
            {"outcome": "decline", "notes": "", "reviewed_by": "Priya Nair"},
        )
        is None
    )
    request = store.get_request("REQ-7001")
    assert request is not None
    assert request["status"] == "decided"
    assert request["recommendation"] == RECOMMENDATION
    assert request["review"]["outcome"] == "approve"
    stages = [step["stage"] for step in request["steps"]]
    assert stages.count("preparing_recommendation") == 1
    assert stages.count("completed") == 1


def test_restart_keeps_requests_in_progress_and_preserves_running_run(store):
    run_id = store.create_run(5)
    store.start_request("REQ-7001")
    store.initialize()
    request = store.get_request("REQ-7001")
    run = store.current_run()
    assert request is not None
    assert request["status"] == "in_progress"
    assert run is not None
    assert run["run_id"] == run_id
    assert run["status"] == "running"


def test_reseed_rebuilds_every_request_as_waiting(store):
    store.start_request("REQ-7001")
    store.save_recommendation("REQ-7001", RECOMMENDATION)
    store.create_run(2)
    store.reseed()
    assert {item["status"] for item in store.list_requests()} == {"waiting"}
    assert len(store.list_requests()) == 9
    assert store.current_run() is None


def test_postgres_engine_uses_undercover_schema_and_caps_connections(
    monkeypatch, tmp_path
):
    captured: dict[str, object] = {}

    def fake_create_engine(url: str, **kwargs: object):
        captured["url"] = url
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr("backend.store.create_engine", fake_create_engine)
    monkeypatch.delenv("SHOPPIE_DATABASE_PATH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://postgres:pw@127.0.0.1:5432/postgres")
    store = Store()

    assert store.is_postgres is True
    assert captured["url"] == "postgresql+psycopg://postgres:pw@127.0.0.1:5432/postgres"
    assert captured["pool_size"] == MAX_POOL_SIZE
    assert captured["max_overflow"] == MAX_OVERFLOW
    assert MAX_POOL_SIZE + MAX_OVERFLOW <= 4
    assert captured["connect_args"] == {"options": f"-c search_path={SCHEMA}"}
    assert sqlalchemy_postgres_url("postgresql://u:p@h/db") == (
        "postgresql+psycopg://u:p@h/db"
    )
    assert sqlalchemy_postgres_url("sqlite:///test.db") == "sqlite:///test.db"

    captured.clear()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "shoppie-backend")
    Store()
    assert captured["poolclass"] is NullPool
    assert "pool_size" not in captured
    assert captured["connect_args"] == {
        "options": f"-c search_path={SCHEMA}",
        "prepare_threshold": None,
    }
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME")

    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "seeded.sqlite3"))
    monkeypatch.setattr("backend.seed.load_dotenv", lambda **kwargs: False)
    monkeypatch.setattr(
        "backend.store.create_engine", __import__("sqlalchemy").create_engine
    )
    assert "seeded.sqlite3" in seed_database()


def test_runs_are_one_at_a_time_and_keep_a_to_do_list(client):
    started = client.post("/api/runs", json={"cap": 5})
    second = client.post("/api/runs", json={"cap": 5})
    run_id = started.json()["run_id"]
    todos = [
        {"request_id": "REQ-7001", "title": "Cancellation", "status": "to_do"},
        {"request_id": "REQ-7002", "title": "Return", "status": "to_do"},
    ]
    written = client.put(
        f"/api/runs/{run_id}/todos", json={"todos": todos}, headers=AGENT_HEADERS
    )
    ticked = client.post(
        f"/api/runs/{run_id}/todos/REQ-7002",
        json={"status": "done"},
        headers=AGENT_HEADERS,
    )
    added = client.post(
        f"/api/runs/{run_id}/todos/REQ-7003",
        json={"status": "left_for_support"},
        headers=AGENT_HEADERS,
    )
    current = client.get("/api/runs/current").json()

    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert second.status_code == 409
    assert written.status_code == 200
    assert ticked.status_code == 200
    assert added.status_code == 200
    assert current["run_id"] == run_id
    assert current["cap"] == 5
    assert [(t["request_id"], t["status"]) for t in current["todos"]] == [
        ("REQ-7001", "to_do"),
        ("REQ-7002", "done"),
        ("REQ-7003", "left_for_support"),
    ]


def test_to_do_list_is_refused_once_a_run_has_finished(client):
    run_id = client.post("/api/runs", json={"cap": 1}).json()["run_id"]
    app.state.store.finish_run(run_id, "finished", "Done.")
    written = client.put(
        f"/api/runs/{run_id}/todos", json={"todos": []}, headers=AGENT_HEADERS
    )
    ticked = client.post(
        f"/api/runs/{run_id}/todos/REQ-7001",
        json={"status": "done"},
        headers=AGENT_HEADERS,
    )
    assert written.status_code == 409
    assert ticked.status_code == 409
    assert client.post("/api/runs", json={"cap": 1}).status_code == 200


def test_a_run_needs_a_cap_of_at_least_one(client):
    assert client.post("/api/runs", json={"cap": 0}).status_code == 422
    assert client.get("/api/runs/current").json() is None


def test_waiting_requests_hold_no_customer_or_order_fields(client):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    waiting = client.get("/api/requests/waiting", headers=AGENT_HEADERS).json()
    assert len(waiting) == 8
    assert "REQ-7001" not in {item["request_id"] for item in waiting}
    assert set(waiting[0]) == {"request_id", "type", "created_at"}


def test_agent_operations_start_save_and_fail_requests(client):
    started = client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    again = client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    missing = client.post("/api/requests/REQ-0000/start", headers=AGENT_HEADERS)
    calc = client.post(
        "/api/requests/REQ-7001/refund",
        json={
            "reason": "cancellation",
            "lines": [{"item_id": "ITM-10001-1", "quantity": 1}],
        },
        headers=AGENT_HEADERS,
    )
    payout = calc.json()["refund"]
    rec = {**RECOMMENDATION, "refund_amount": payout}
    saved = client.post(
        "/api/requests/REQ-7001/recommendation",
        json=rec,
        headers=AGENT_HEADERS,
    )
    duplicate = client.post(
        "/api/requests/REQ-7001/recommendation",
        json=rec,
        headers=AGENT_HEADERS,
    )
    conflicting = client.post(
        "/api/requests/REQ-7001/recommendation",
        json={**rec, "outcome": "decline", "refund_amount": 0.0},
        headers=AGENT_HEADERS,
    )
    client.post("/api/requests/REQ-7002/start", headers=AGENT_HEADERS)
    failed = client.post("/api/requests/REQ-7002/failure", headers=AGENT_HEADERS)
    not_running = client.post("/api/requests/REQ-7003/failure", headers=AGENT_HEADERS)

    assert started.json() == {
        "request_id": "REQ-7001",
        "status": "in_progress",
        "attempt": 1,
    }
    assert again.status_code == 409
    assert missing.status_code == 404
    assert saved.json()["status"] == "waiting_for_agent_review"
    assert duplicate.status_code == 200
    assert conflicting.status_code == 409
    assert failed.json() == {"request_id": "REQ-7002", "status": "waiting"}
    assert not_running.status_code == 409


def test_agent_progress_steps_need_a_request_in_progress(client):
    invalid = client.post(
        "/api/requests/REQ-7001/steps",
        json={"stage": "finished"},
        headers=AGENT_HEADERS,
    )
    idle = client.post(
        "/api/requests/REQ-7001/steps",
        json={"stage": "checking_policy"},
        headers=AGENT_HEADERS,
    )
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    recorded = client.post(
        "/api/requests/REQ-7001/steps",
        json={"stage": "checking_policy"},
        headers=AGENT_HEADERS,
    )
    assert invalid.status_code == 422
    assert idle.status_code == 409
    assert recorded.status_code == 200


def test_support_review_hands_off_to_agent_or_decides_left_for_support(
    client, monkeypatch
):
    resumed: list[str] = []
    monkeypatch.setattr(main, "notify_agent_resume", resumed.append)

    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    while_working = client.post(
        "/api/requests/REQ-7001/review",
        json={"outcome": "approve", "notes": "", "reviewed_by": "Priya Nair"},
    )
    assert while_working.status_code == 409
    early = client.post(
        "/api/requests/REQ-7002/review",
        json={"outcome": "approve", "notes": "", "reviewed_by": "Priya Nair"},
    )
    calc = client.post(
        "/api/requests/REQ-7001/refund",
        json={
            "reason": "cancellation",
            "lines": [{"item_id": "ITM-10001-1", "quantity": 1}],
        },
        headers=AGENT_HEADERS,
    )
    payout = calc.json()["refund"]
    client.post(
        "/api/requests/REQ-7001/recommendation",
        json={**RECOMMENDATION, "refund_amount": payout},
        headers=AGENT_HEADERS,
    )
    review_payload = {
        "outcome": "approve",
        "notes": "Confirmed by support.",
        "reviewed_by": "Priya Nair",
    }
    response = client.post("/api/requests/REQ-7001/review", json=review_payload)
    repeat = client.post("/api/requests/REQ-7001/review", json=review_payload)
    conflicting = client.post(
        "/api/requests/REQ-7001/review",
        json={"outcome": "approve", "notes": "", "reviewed_by": "Priya Nair"},
    )
    signed_off = client.get("/api/requests/REQ-7001").json()
    completed = client.post(
        "/api/requests/REQ-7001/complete",
        json={
            **review_payload,
            "reviewed_at": signed_off["review"]["reviewed_at"],
        },
        headers=AGENT_HEADERS,
    )
    detail = client.get("/api/requests/REQ-7001").json()

    assert early.status_code == 409
    assert response.status_code == 200
    assert response.json()["status"] == "decided"
    assert repeat.status_code == 200
    assert conflicting.status_code == 409
    assert resumed == ["REQ-7001", "REQ-7001"]
    assert completed.status_code == 200
    assert detail["status"] == "decided"
    assert detail["review"]["notes"] == "Confirmed by support."
    assert detail["review"]["reviewed_by"] == "Priya Nair"
    assert [step["stage"] for step in detail["steps"]].count("completed") == 1
    assert "action_result" not in detail

    client.post("/api/requests/REQ-7003/start", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7003/failure", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7003/start", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7003/failure", headers=AGENT_HEADERS)
    left_review = client.post(
        "/api/requests/REQ-7003/review",
        json={"outcome": "decline", "notes": "Manual decision.", "reviewed_by": "Dev"},
    )
    assert left_review.status_code == 200
    assert left_review.json()["status"] == "decided"


def test_start_run_posts_work_run_with_bearer_and_undoes_refused_or_unreachable(
    tmp_path, monkeypatch
):
    db = tmp_path / "gate.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("UNDERCOVER_AGENT", "http://agent.test/")
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, status_code: int, body: dict[str, Any]) -> None:
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def json(self) -> dict[str, Any]:
            return self._body

    def accept_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"accepted": True})

    with TestClient(app) as test_client:
        monkeypatch.setattr(agent_service.httpx, "post", accept_post)
        started = test_client.post("/api/runs", json={"cap": 2})
        assert started.status_code == 200
        run_id = started.json()["run_id"]
        assert calls[0]["url"] == "http://agent.test/invocations"
        assert calls[0]["json"] == {"operation": "work_run", "run_id": run_id}
        assert calls[0]["headers"] == {"Authorization": f"Bearer {TEST_API_KEY}"}

        dup = test_client.post("/api/runs", json={"cap": 2})
        assert dup.status_code == 409
        assert run_id in dup.json()["detail"]

        # Finish run to free the gate, then simulate unreachable agent
        test_client.post(
            f"/api/runs/{run_id}/finish",
            json={"status": "finished", "summary": "Done"},
            headers=AGENT_HEADERS,
        )

        def unreachable(url: str, **kwargs: Any) -> None:
            raise agent_service.httpx.ConnectError("connection refused")

        monkeypatch.setattr(agent_service.httpx, "post", unreachable)
        failed_start = test_client.post("/api/runs", json={"cap": 2})
        assert failed_start.status_code == 502
        failed_run = test_client.get("/api/runs/current").json()
        assert failed_run["status"] == "failed"
        assert "connection refused" in failed_run["stop_reason"]

        # Refused clears gate too; a subsequent accepted start succeeds
        monkeypatch.setattr(
            agent_service.httpx,
            "post",
            lambda url, **kw: FakeResponse(200, {"refused": "Busy"}),
        )
        refused_start = test_client.post("/api/runs", json={"cap": 2})
        assert refused_start.status_code == 409
        assert test_client.get("/api/runs/current").json()["stop_reason"] == "Busy"

        monkeypatch.setattr(agent_service.httpx, "post", accept_post)
        recovered = test_client.post("/api/runs", json={"cap": 3})
        assert recovered.status_code == 200


def test_start_run_timeout_leaves_run_running_and_gate_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "timeout.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_API_KEY)

    def timeout(url: str, **kwargs: Any) -> None:
        raise agent_service.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(agent_service.httpx, "post", timeout)
    with TestClient(app) as test_client:
        timed = test_client.post("/api/runs", json={"cap": 1})
        assert timed.status_code == 504
        assert "Reload the page" in timed.json()["detail"]
        current = test_client.get("/api/runs/current").json()
        assert current["status"] == "running"
        assert test_client.post("/api/runs", json={"cap": 1}).status_code == 409


def test_concurrent_create_run_only_allows_one_winner(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.create_run, [2, 2]))
    winners = [run_id for run_id in results if run_id is not None]
    assert len(winners) == 1


def test_stale_run_recovered_after_five_minutes_while_heartbeat_keeps_alive(client):
    store = app.state.store
    first_id = client.post("/api/runs", json={"cap": 2}).json()["run_id"]
    old_time = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with store.engine.begin() as conn:
        conn.execute(
            update(runs).where(runs.c.run_id == first_id).values(started_at=old_time)
        )
    # Heartbeat within 5 minutes keeps old started_at run alive
    beat = client.post(f"/api/runs/{first_id}/heartbeat", headers=AGENT_HEADERS)
    assert beat.status_code == 200
    assert client.post("/api/runs", json={"cap": 2}).status_code == 409

    # Quiet heartbeat for > 5 minutes lets new run recover it with stop_reason
    with store.engine.begin() as conn:
        conn.execute(
            update(runs)
            .where(runs.c.run_id == first_id)
            .values(last_heartbeat_at=old_time)
        )
    second = client.post("/api/runs", json={"cap": 2})
    assert second.status_code == 200
    old_run = client.get(f"/api/runs/{first_id}").json()
    assert old_run["status"] == "failed"
    assert old_run["stop_reason"] == STALE_WORK_REASON
    assert old_run["summary"] == STALE_WORK_REASON
    assert (
        client.post(
            f"/api/runs/{first_id}/heartbeat", headers=AGENT_HEADERS
        ).status_code
        == 409
    )
    assert client.get("/api/runs/RUN-NOPE").status_code == 404
    assert (
        client.post("/api/runs/RUN-NOPE/heartbeat", headers=AGENT_HEADERS).status_code
        == 404
    )


def test_finish_run_needs_the_key_and_a_running_run(client):
    run_id = client.post("/api/runs", json={"cap": 1}).json()["run_id"]
    payload = {"status": "finished", "summary": "Worked 1."}
    unauthenticated = client.post(f"/api/runs/{run_id}/finish", json=payload)
    finished = client.post(
        f"/api/runs/{run_id}/finish", json=payload, headers=AGENT_HEADERS
    )
    again = client.post(
        f"/api/runs/{run_id}/finish", json=payload, headers=AGENT_HEADERS
    )
    current = client.get("/api/runs/current").json()

    assert unauthenticated.status_code == 401
    assert finished.json() == {"run_id": run_id, "status": "finished"}
    assert again.status_code == 409
    assert current["status"] == "finished"
    assert current["summary"] == "Worked 1."


def test_instructions_cover_the_queue_and_a_request(client):
    instructions = client.get("/api/agent/instructions").json()
    assert set(instructions) == {"queue", "request"}
    assert "running the support queue" in instructions["queue"]
    assert instructions["request"].startswith("You are the Shoppie Agent")
    assert "data/evals" not in instructions["request"]


def test_support_review_warns_when_agent_unreachable_or_times_out(
    client, monkeypatch, caplog
):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    calc = client.post(
        "/api/requests/REQ-7001/refund",
        json={
            "reason": "cancellation",
            "lines": [{"item_id": "ITM-10001-1", "quantity": 1}],
        },
        headers=AGENT_HEADERS,
    )
    client.post(
        "/api/requests/REQ-7001/recommendation",
        json={**RECOMMENDATION, "refund_amount": calc.json()["refund"]},
        headers=AGENT_HEADERS,
    )

    def timeout(url: str, **kwargs: object):
        raise agent_service.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(agent_service.httpx, "post", timeout)
    with caplog.at_level("WARNING"):
        res = client.post(
            "/api/requests/REQ-7001/review",
            json={"outcome": "approve", "notes": "Ok", "reviewed_by": "Priya Nair"},
        )

    assert res.status_code == 200
    assert res.json()["request_id"] == "REQ-7001"
    assert res.json()["status"] == "decided"
    assert res.json()["review"]["reviewed_by"] == "Priya Nair"
    assert any("REQ-7001" in rec.getMessage() for rec in caplog.records)


def _recommend_req_7001(client: TestClient) -> dict[str, Any]:
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    calc = client.post(
        "/api/requests/REQ-7001/refund",
        json={
            "reason": "cancellation",
            "lines": [{"item_id": "ITM-10001-1", "quantity": 1}],
        },
        headers=AGENT_HEADERS,
    )
    payout = calc.json()["refund"]
    return client.post(
        "/api/requests/REQ-7001/recommendation",
        json={**RECOMMENDATION, "refund_amount": payout},
        headers=AGENT_HEADERS,
    ).json()


def test_send_back_refusals_and_validation(client, monkeypatch):
    monkeypatch.setattr(main, "notify_agent_resume", lambda rid: None)

    waiting = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "Check item", "sent_back_by": "Priya Nair"},
    )
    assert waiting.status_code == 409

    with_key = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "Check item", "sent_back_by": "Priya Nair"},
        headers=AGENT_HEADERS,
    )
    assert with_key.status_code == 401

    client.post("/api/requests/REQ-7002/start", headers=AGENT_HEADERS)
    in_prog = client.post(
        "/api/requests/REQ-7002/send_back",
        json={"note": "Check item", "sent_back_by": "Priya Nair"},
    )
    assert in_prog.status_code == 409

    client.post("/api/requests/REQ-7002/failure", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7002/start", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7002/failure", headers=AGENT_HEADERS)
    left_for_support = client.post(
        "/api/requests/REQ-7002/send_back",
        json={"note": "No recommendation", "sent_back_by": "Priya Nair"},
    )
    assert left_for_support.status_code == 409

    client.post("/api/requests/REQ-7901/start", headers=AGENT_HEADERS)
    client.post(
        "/api/requests/REQ-7901/recommendation",
        json={
            "outcome": "request_information",
            "rationale": "Instruction found.",
            "policy_citations": ["Security check"],
            "next_action": "Hold.",
            "priority_human_review": True,
            "decided_by": "Security check",
        },
        headers=AGENT_HEADERS,
    )
    sec_refusal = client.post(
        "/api/requests/REQ-7901/send_back",
        json={"note": "Override security check", "sent_back_by": "Priya Nair"},
    )
    assert sec_refusal.status_code == 409

    _recommend_req_7001(client)
    blank_note = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "   ", "sent_back_by": "Priya Nair"},
    )
    too_long = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "x" * 1001, "sent_back_by": "Priya Nair"},
    )
    blank_sender = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "Please recheck.", "sent_back_by": " "},
    )
    assert blank_note.status_code == 422
    assert too_long.status_code == 422
    assert blank_sender.status_code == 422

    orig_get = app.state.store.get_request
    monkeypatch.setattr(
        app.state.store,
        "get_request",
        lambda rid: (
            {**(orig_get(rid) or {}), "evaluation_id": "eval-1"}
            if rid == "REQ-7001"
            else orig_get(rid)
        ),
    )
    eval_refusal = client.post(
        "/api/requests/REQ-7001/send_back",
        json={"note": "Eval copy", "sent_back_by": "Priya Nair"},
    )
    assert eval_refusal.status_code == 409

    agent_ops = {
        op["operationId"]
        for path_item in client.get("/api/agent/openapi.json").json()["paths"].values()
        for op in path_item.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert len(agent_ops) == 6
    assert "send_back_request" not in agent_ops
    assert "take_sent_back" not in agent_ops


def test_send_back_archives_history_blocks_concurrent_review_and_warns_offline(
    client, monkeypatch, caplog
):
    first = _recommend_req_7001(client)["decision"]

    def unreachable(url: str, **kwargs: object):
        raise agent_service.httpx.ConnectError("connection refused")

    monkeypatch.setattr(agent_service.httpx, "post", unreachable)
    with caplog.at_level("WARNING"):
        sent = client.post(
            "/api/requests/REQ-7001/send_back",
            json={
                "note": "Recalculate with delivery fee checked.",
                "sent_back_by": "Priya Nair",
            },
        )
    assert sent.status_code == 200
    body = sent.json()
    assert body["status"] == "sent_back"
    assert body["recommendation"] is None
    assert len(body["sent_back"]) == 1
    assert body["sent_back"][0]["note"] == "Recalculate with delivery fee checked."
    assert body["sent_back"][0]["sent_back_by"] == "Priya Nair"
    assert body["sent_back"][0]["decision"]["payout"] == first["payout"]
    assert body["steps"][-1]["stage"] == "sent_back"
    assert any("REQ-7001" in r.getMessage() for r in caplog.records)

    waiting_ids = {
        item["request_id"]
        for item in client.get("/api/requests/waiting", headers=AGENT_HEADERS).json()
    }
    assert "REQ-7001" in waiting_ids

    sign_off_while_sent_back = client.post(
        "/api/requests/REQ-7001/review",
        json={"outcome": "approve", "notes": "Too late", "reviewed_by": "Priya Nair"},
    )
    assert sign_off_while_sent_back.status_code == 409

    store = app.state.store
    assert store.attempts_calculation("REQ-7001", 1) is None

    unauth_take = client.post(
        "/api/requests/REQ-7001/take_sent_back", json={"owner": "resume"}
    )
    assert unauth_take.status_code == 401
    taken = client.post(
        "/api/requests/REQ-7001/take_sent_back",
        json={"owner": "resume"},
        headers=AGENT_HEADERS,
    )
    taken_second = client.post(
        "/api/requests/REQ-7001/take_sent_back",
        json={"owner": "run"},
        headers=AGENT_HEADERS,
    )
    assert taken.status_code == 200
    assert taken_second.status_code == 409

    hb = client.post("/api/requests/REQ-7001/heartbeat", headers=AGENT_HEADERS)
    assert hb.status_code == 200
    assert client.get("/api/requests/REQ-7001").json()["resume_heartbeat_at"]

    client.post(
        "/api/requests/REQ-7003/start",
        headers=AGENT_HEADERS,
    )
    assert (
        client.post(
            "/api/requests/REQ-7003/take_sent_back",
            json={"owner": "run"},
            headers=AGENT_HEADERS,
        ).status_code
        == 409
    )

    # Atomic mutex between store.record_review and store.send_back_request
    store.save_recommendation(
        "REQ-7003",
        {**RECOMMENDATION, "decision": "decline", "payout": 0.0, "refund_amount": 0.0},
    )
    assert (
        store.record_review(
            "REQ-7003", {"outcome": "decline", "notes": "", "reviewed_by": "Priya Nair"}
        )
        == "decided"
    )
    assert not store.send_back_request("REQ-7003", "Too late", "Priya Nair")
    assert (
        client.post(
            "/api/requests/REQ-7003/heartbeat", headers=AGENT_HEADERS
        ).status_code
        == 409
    )
