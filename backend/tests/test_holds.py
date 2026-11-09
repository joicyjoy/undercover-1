"""Tests for holding an approved request whose completion was refused by Gateway."""

from fastapi.testclient import TestClient

from backend.main import app

KEY = "test-agent-secret-key-0123456789abcdef"
HEADERS = {"X-API-Key": KEY}


def test_hold_payment_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "holds.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", KEY)
    with TestClient(app) as client:
        res = client.post(
            "/api/requests/REQ-7001/hold", json={"reason": "Denied by policy"}
        )
        assert res.status_code == 401


def test_hold_payment_requires_approved_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "holds.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", KEY)
    with TestClient(app) as client:
        # Not yet started or recommended -> outcome is not approve
        res = client.post(
            "/api/requests/REQ-7001/hold",
            json={"reason": "Denied by policy"},
            headers=HEADERS,
        )
        assert res.status_code == 409
        assert "Request is not approved" in res.json()["detail"]


def test_hold_payment_moves_approved_request_to_waiting_for_sign_off(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "holds.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", KEY)
    with TestClient(app) as client:
        client.post("/api/requests/REQ-7001/start", headers=HEADERS)
        client.post(
            "/api/requests/REQ-7001/recommendation",
            headers=HEADERS,
            json={
                "outcome": "approve",
                "rationale": "Eligible for refund",
                "policy_citations": ["cancellation"],
                "next_action": "Support agent review",
                "priority_human_review": False,
            },
        )
        res = client.post(
            "/api/requests/REQ-7001/hold",
            json={"reason": "Payout over limit"},
            headers=HEADERS,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "waiting_for_agent_review"
        assert data["recommendation"]["gateway_refusal"] == "Payout over limit"
        refusal_steps = [s for s in data["steps"] if s["stage"] == "refusal"]
        assert len(refusal_steps) >= 1
        assert "Payout over limit" in refusal_steps[-1]["message"]


def test_hold_payment_rejects_declined_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "holds.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", KEY)
    with TestClient(app) as client:
        client.post("/api/requests/REQ-7001/start", headers=HEADERS)
        client.post(
            "/api/requests/REQ-7001/recommendation",
            headers=HEADERS,
            json={
                "outcome": "decline",
                "rationale": "Not eligible",
                "policy_citations": ["cancellation"],
                "next_action": "Support agent review",
                "priority_human_review": False,
            },
        )
        res = client.post(
            "/api/requests/REQ-7001/hold",
            json={"reason": "Too late"},
            headers=HEADERS,
        )
        assert res.status_code == 409
        assert "not approved" in res.json()["detail"].lower()
