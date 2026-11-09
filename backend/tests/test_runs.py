import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from backend import main
from backend.main import app
from backend.seed import seed_database
from backend.store import (
    MAX_OVERFLOW,
    MAX_POOL_SIZE,
    SCHEMA,
    Store,
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
    monkeypatch.setattr(main, "run_in_background", lambda *args: None)
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


def test_restart_keeps_requests_in_progress_and_closes_stopped_runs(store):
    run_id = store.create_run(5)
    store.start_request("REQ-7001")
    store.initialize()
    request = store.get_request("REQ-7001")
    run = store.current_run()
    assert request is not None
    assert request["status"] == "in_progress"
    assert run is not None
    assert run["run_id"] == run_id
    assert run["status"] == "failed"


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


def test_background_run_is_handed_to_the_agent_service_with_the_key(store, monkeypatch):
    calls: list[dict[str, object]] = []

    class Accepted:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, **kwargs: object) -> Accepted:
        calls.append({"url": url, **kwargs})
        return Accepted()

    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("UNDERCOVER_AGENT", "http://agent.test/")
    monkeypatch.setattr(main.httpx, "post", fake_post)
    run_id = store.create_run(2)
    assert run_id is not None
    main.run_in_background(store, run_id, 2)

    assert calls[0]["url"] == f"http://agent.test/runs/{run_id}/work"
    assert calls[0]["json"] == {"cap": 2}
    assert calls[0]["headers"] == AGENT_HEADERS
    run = store.current_run()
    assert run is not None
    assert run["status"] == "running"


def test_background_run_is_marked_failed_when_the_agent_service_is_unreachable(
    store, monkeypatch
):
    def unreachable(url: str, **kwargs: object):
        raise main.httpx.ConnectError("connection refused")

    monkeypatch.setattr(main.httpx, "post", unreachable)
    run_id = store.create_run(2)
    assert run_id is not None
    main.run_in_background(store, run_id, 2)
    run = store.current_run()
    assert run is not None
    assert run["status"] == "failed"
    assert "agent service failed" in run["summary"]


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
