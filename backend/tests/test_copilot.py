"""Phase 1 tests for handler roles, tokens, Copilot storage, and metrics."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from backend import agent_service
from backend.agent_service import AgentCallResult
from backend.handlers import HANDLERS, create_handler_token, verify_handler_token
from backend.main import app
from backend.store import Store, copilot_conversations, copilot_turns


def test_handler_identity_dev_and_entra_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "handlers.sqlite3"))
    with TestClient(app) as client:
        default_me = client.get("/api/copilot/me")
        marcus_dev = client.get(
            "/api/copilot/me", headers={"X-Shoppie-Handler": "HND-02"}
        )
        entra_marcus = client.get(
            "/api/copilot/me",
            headers={
                "X-MS-CLIENT-PRINCIPAL-ID": "aad-2",
                "X-MS-CLIENT-PRINCIPAL-NAME": "marcus.oyelaran@shoppie.example",
            },
        )
        entra_dana = client.get(
            "/api/copilot/me",
            headers={
                "X-MS-CLIENT-PRINCIPAL-ID": "aad-1",
                "X-MS-CLIENT-PRINCIPAL-NAME": "dana.whitfield@shoppie.example",
            },
        )

    assert default_me.status_code == 200
    assert default_me.json()["handler"]["handler_id"] == "HND-01"
    assert default_me.json()["handler"]["can_sign_off_over_limit"] is True
    assert len(default_me.json()["dev_handlers"]) == 2

    assert marcus_dev.status_code == 200
    assert marcus_dev.json()["handler"]["handler_id"] == "HND-02"
    assert marcus_dev.json()["handler"]["can_sign_off_over_limit"] is False

    assert entra_marcus.status_code == 200
    assert entra_marcus.json()["handler"]["auth_mode"] == "entra"
    assert entra_marcus.json()["handler"]["can_sign_off_over_limit"] is False
    assert entra_marcus.json()["dev_handlers"] == []

    assert entra_dana.status_code == 200
    assert entra_dana.json()["handler"]["can_sign_off_over_limit"] is True


def test_ephemeral_handler_token_isolation_and_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "tokens.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", "agent-key-only-secret")
    token = create_handler_token(HANDLERS["HND-02"], ttl_seconds=60)
    verified = verify_handler_token(token)
    assert verified.handler_id == "HND-02"

    expired = create_handler_token(HANDLERS["HND-01"], ttl_seconds=-10)
    with TestClient(app) as client:
        ok = client.get("/api/copilot/me", headers={"Authorization": f"Bearer {token}"})
        bad_agent = client.get(
            "/api/copilot/me",
            headers={"Authorization": "Bearer agent-key-only-secret"},
        )
        expired_resp = client.get(
            "/api/copilot/me", headers={"Authorization": f"Bearer {expired}"}
        )
        start_with_handler = client.post(
            "/api/requests/REQ-7001/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        me_with_api_key = client.get(
            "/api/copilot/me", headers={"X-API-Key": "agent-key-only-secret"}
        )

    assert ok.status_code == 200
    assert ok.json()["handler"]["handler_id"] == "HND-02"
    assert bad_agent.status_code == 401
    assert expired_resp.status_code == 401
    assert start_with_handler.status_code == 401
    assert me_with_api_key.status_code == 401


def test_over_limit_review_requires_over_limit_permission(tmp_path, monkeypatch):
    db = tmp_path / "review_perm.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    monkeypatch.setenv("UNDERCOVER_API_KEY", "agent-secret")
    store = Store(db)
    store.initialize()
    assert store.start_request("REQ-7001") == 1
    assert store.save_recommendation(
        "REQ-7001",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 15000.0,
            "refund_amount": 15000.0,
            "section": "Section 1",
            "reason": "Escalated payout",
            "decided_by": "Shoppie Agent",
        },
    )

    marcus_token = create_handler_token(HANDLERS["HND-02"])
    dana_token = create_handler_token(HANDLERS["HND-01"])
    with TestClient(app) as client:
        denied = client.post(
            "/api/requests/REQ-7001/review",
            json={"outcome": "approve", "notes": "Approve", "reviewed_by": "Marcus"},
            headers={"Authorization": f"Bearer {marcus_token}"},
        )
        allowed = client.post(
            "/api/requests/REQ-7001/review",
            json={"outcome": "approve", "notes": "Approve", "reviewed_by": "Dana"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )

    assert denied.status_code == 403
    assert "10,000.00" in denied.json()["detail"]
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "decided"


def test_overview_metrics_customer_history_and_quality_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "metrics.sqlite3"))
    with TestClient(app) as client:
        settings = client.get("/api/settings")
        overview = client.get("/api/copilot/overview")
        quality = client.post("/api/quality_runs", json={"scope": "sample"})
        current_q = client.get("/api/quality_runs/current")
        queue = client.get("/api/requests").json()
        cid = queue[0]["customer_id"]
        customer = client.get(f"/api/copilot/customers/{cid}")

    assert settings.status_code == 200
    assert settings.json()["approval_limit"] == 10000.0
    assert settings.json()["currency"] == "INR"

    assert overview.status_code == 200
    criteria = overview.json()["success_criteria"]
    assert criteria["over_limit_sign_offs"]["met"] is True
    assert "10,000.00" in criteria["over_limit_sign_offs"]["reason"]
    assert criteria["accuracy"]["met"] is None

    assert quality.status_code == 200
    assert quality.json()["total_cases"] == 5
    assert current_q.status_code == 200
    assert current_q.json()["benchmark_id"] == quality.json()["benchmark_id"]

    assert customer.status_code == 200
    assert customer.json()["customer_id"] == cid
    assert customer.json()["requests"]


def test_conversation_isolation_turns_steps_and_stale_timeout(tmp_path, monkeypatch):
    db = tmp_path / "conv.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    dispatched: list[tuple[dict[str, object], str]] = []

    def fake_call_copilot(
        payload: dict[str, object], conversation_id: str = ""
    ) -> AgentCallResult:
        dispatched.append((payload, conversation_id))
        return AgentCallResult("accepted")

    monkeypatch.setattr(agent_service, "call_copilot", fake_call_copilot)
    dana_token = create_handler_token(HANDLERS["HND-01"])
    marcus_token = create_handler_token(HANDLERS["HND-02"])

    with TestClient(app) as client:
        created = client.post(
            "/api/copilot/conversations",
            json={"title": "Morning check"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        conv_id = created.json()["conversation_id"]

        other_read = client.get(
            f"/api/copilot/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {marcus_token}"},
        )
        turn = client.post(
            f"/api/copilot/conversations/{conv_id}/turns",
            json={"message": "What needs my attention today?"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        turn_id = turn.json()["turn"]["turn_id"]

        concurrent = client.post(
            f"/api/copilot/conversations/{conv_id}/turns",
            json={"message": "Second message while busy"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        step = client.post(
            f"/api/copilot/conversations/{conv_id}/steps",
            json={
                "turn_id": turn_id,
                "kind": "tool",
                "tool_name": "get_copilot_overview",
                "summary": "Checked queue and waiting requests.",
                "detail": {"waiting": 9},
                "duration_ms": 42,
            },
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        paused = client.post(
            f"/api/copilot/conversations/{conv_id}/turns/{turn_id}/complete",
            json={
                "status": "waiting_confirmation",
                "confirmation": {
                    "request_id": "REQ-7001",
                    "customer_name": "Rohan Kulkarni",
                    "payout": 2500.0,
                },
            },
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        confirmed = client.post(
            f"/api/copilot/conversations/{conv_id}/turns/{turn_id}/confirm",
            json={"approved": True, "note": "Verified"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        done = client.post(
            f"/api/copilot/conversations/{conv_id}/turns/{turn_id}/complete",
            json={"status": "done", "assistant_message": "Signed off REQ-7001."},
            headers={"Authorization": f"Bearer {dana_token}"},
        )

    assert created.status_code == 200
    assert other_read.status_code == 403
    assert turn.status_code == 200
    assert concurrent.status_code == 409
    assert step.status_code == 200
    assert step.json()["seq"] == 1
    assert paused.json()["status"] == "waiting_confirmation"
    assert confirmed.status_code == 200
    assert confirmed.json()["conversation"]["status"] == "working"
    assert done.json()["status"] == "done"
    assert done.json()["turns"][0]["assistant_message"] == "Signed off REQ-7001."

    # Verify stale turn (>20 min) transitions to stopped cleanly
    store = Store(db)
    old_iso = (datetime.now(UTC) - timedelta(minutes=21)).isoformat()
    started = store.start_copilot_turn(conv_id, "HND-01", "Check again")
    assert started is not None
    with store.engine.begin() as conn:
        conn.execute(
            update(copilot_conversations)
            .where(copilot_conversations.c.conversation_id == conv_id)
            .values(turn_started_at=old_iso)
        )
        conn.execute(
            update(copilot_turns)
            .where(copilot_turns.c.turn_id == started["turn_id"])
            .values(created_at=old_iso)
        )
    refreshed = store.get_conversation(conv_id)
    assert refreshed is not None
    assert refreshed["status"] == "stopped"
    matching = next(t for t in refreshed["turns"] if t["turn_id"] == started["turn_id"])
    assert matching["status"] == "stopped"
    assert len(dispatched) == 2
    assert dispatched[0][0]["action"] == "copilot_turn"
    assert dispatched[1][0]["action"] == "copilot_resume"


def test_copilot_turn_dispatch_payload_and_failure_fallbacks(tmp_path, monkeypatch):
    db = tmp_path / "dispatch.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    dispatched: list[tuple[dict[str, object], str]] = []
    next_result = AgentCallResult("did_not_take", "Copilot down")

    def fake_call_copilot(
        payload: dict[str, object], conversation_id: str = ""
    ) -> AgentCallResult:
        dispatched.append((payload, conversation_id))
        return next_result

    monkeypatch.setattr(agent_service, "call_copilot", fake_call_copilot)
    dana_token = create_handler_token(HANDLERS["HND-01"])

    with TestClient(app) as client:
        conv_id = client.post(
            "/api/copilot/conversations",
            json={"title": "Dispatch check"},
            headers={"Authorization": f"Bearer {dana_token}"},
        ).json()["conversation_id"]

        failed_start = client.post(
            f"/api/copilot/conversations/{conv_id}/turns",
            json={
                "message": "Inspect REQ-9001",
                "page_path": "/requests",
                "selected_request_id": "REQ-9001",
            },
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        assert failed_start.status_code == 200
        body = failed_start.json()
        assert body["call_state"] == "did_not_take"
        assert body["turn"]["status"] == "failed"
        assert body["turn"]["error"] == "Copilot down"
        payload0, cid0 = dispatched[0]
        assert cid0 == conv_id
        assert payload0["page_path"] == "/requests"
        assert payload0["selected_request_id"] == "REQ-9001"
        verified = verify_handler_token(str(payload0["handler_token"]))
        assert verified is not None
        assert verified.handler_id == "HND-01"

        next_result = AgentCallResult("accepted")
        started = client.post(
            f"/api/copilot/conversations/{conv_id}/turns",
            json={"message": "Sign off REQ-9001"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        turn_id = started.json()["turn"]["turn_id"]
        client.post(
            f"/api/copilot/conversations/{conv_id}/turns/{turn_id}/complete",
            json={
                "status": "waiting_confirmation",
                "confirmation": {
                    "request_id": "REQ-9001",
                    "customer_name": "Test Customer",
                    "payout": 1200.0,
                },
            },
            headers={"Authorization": f"Bearer {dana_token}"},
        )

        next_result = AgentCallResult("refused", "Runtime busy")
        confirm_failed = client.post(
            f"/api/copilot/conversations/{conv_id}/turns/{turn_id}/confirm",
            json={"approved": True, "note": "Go ahead"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        assert confirm_failed.status_code == 200
        confirm_body = confirm_failed.json()
        assert confirm_body["call_state"] == "refused"
        assert confirm_body["conversation"]["status"] == "waiting_confirmation"
        waiting_turn = next(
            t for t in confirm_body["conversation"]["turns"] if t["turn_id"] == turn_id
        )
        assert waiting_turn["status"] == "waiting_confirmation"
        assert waiting_turn["error"] == "Runtime busy"
        assert waiting_turn["confirmation"]["request_id"] == "REQ-9001"
