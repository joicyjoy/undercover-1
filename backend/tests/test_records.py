"""A complete decision record, as claims manual section 14 asks: the calculation
of an approval's payout, who or what made the decision, when, and the manual's
version."""

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from backend import agent_service, main
from backend.context_files import CONTEXT
from backend.main import app
from backend.models import Decision
from backend.store import Store

TEST_API_KEY = "test-agent-secret-key-0123456789abcdef"
AGENT_HEADERS = {"X-API-Key": TEST_API_KEY}
MAKER = "The Shoppie Agent 1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "records.sqlite3")
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", db_path)
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(
        agent_service,
        "call_agent",
        lambda payload: agent_service.AgentCallResult("accepted"),
    )
    with TestClient(app) as test_client:
        yield test_client


def work_out(client: TestClient, request_id: str = "REQ-7001") -> Any:
    """Work out a cancellation refund on REQ-7001."""
    return client.post(
        f"/api/requests/{request_id}/refund",
        json={
            "reason": "cancellation",
            "lines": [{"item_id": "ITM-10001-1", "quantity": 1}],
        },
        headers=AGENT_HEADERS,
    )


def record(client: TestClient, request_id: str = "REQ-7001", **changes: Any) -> Any:
    """Ask to record an approval of 300.00 on REQ-7001 unless told otherwise."""
    body = {
        "decision": "approve",
        "payout": 300.0,
        "section": "cancellation",
        "reason": "Customer requested within cancellation window.",
        "decided_by": MAKER,
    }
    body.update(changes)
    return client.post(
        f"/api/requests/{request_id}/recommendation",
        json=body,
        headers=AGENT_HEADERS,
    )


def test_the_latest_calculation_is_kept_for_the_requests_attempt(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    first = work_out(client, "REQ-7001").json()
    assert first["refund"] == 300.0

    # Read from store directly
    store = Store(main.database_path())
    calc = store.attempts_calculation("REQ-7001", 1)
    assert calc is not None
    assert calc.payout == 300.0
    assert len(calc.lines) == 2


def test_a_new_attempt_starts_with_no_calculation(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    work_out(client, "REQ-7001")
    client.post("/api/requests/REQ-7001/failure", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    response = record(client, "REQ-7001")
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Claims manual section 14: an approval needs the calculation of its payout "
        "for this attempt. Work it out with calculate_payout"
    )


def test_only_a_request_in_progress_has_its_payout_worked_out(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    client.post("/api/requests/REQ-7001/failure", headers=AGENT_HEADERS)
    response = work_out(client, "REQ-7001")
    assert response.status_code == 409
    assert response.json()["detail"] == "Request is not in progress"


def test_a_repeated_approval_is_returned_idempotently(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    work_out(client, "REQ-7001")
    first = record(client, "REQ-7001").json()
    again = record(client, "REQ-7001")
    assert again.status_code == 200
    assert again.json()["decision"]["payout"] == first["decision"]["payout"]
    assert again.json()["decision"]["decision"] == first["decision"]["decision"]


def test_a_request_never_started_has_no_calculation(client: TestClient):
    store = Store(main.database_path())
    assert store.attempts_calculation("REQ-7001", 1) is None


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"reason": " "}, "a decision record needs a reason in plain language"),
        ({"section": ""}, "a decision record needs the section relied on"),
        (
            {"decided_by": ""},
            "a decision record needs who or what made the decision",
        ),
        ({"payout": 0}, "an approval needs a payout above zero"),
        ({"payout": -5}, "an approval needs a payout above zero"),
        ({"payout": 0.004}, "an approval needs a payout above zero"),
        (
            {"decision": "decline", "payout": 50},
            "a decline or a referral has a payout of zero",
        ),
        (
            {"decision": "refer", "payout": 50},
            "a decline or a referral has a payout of zero",
        ),
    ],
)
@pytest.mark.parametrize("request_id", ["REQ-7001", "REQ-9999"])
def test_a_record_that_falls_short_is_refused_before_the_request_is_read(
    client: TestClient, changes: dict[str, Any], missing: str, request_id: str
):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    work_out(client, "REQ-7001")
    response = record(client, request_id, **changes)
    assert response.status_code == 422
    assert response.json()["detail"] == f"Claims manual section 14: {missing}"


def test_a_decision_outside_the_three_is_pydantics_refusal(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    response = record(client, "REQ-7001", decision="maybe")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_an_approval_needs_the_calculations_payout(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    assert (
        record(client, "REQ-7001")
        .json()["detail"]
        .startswith("Claims manual section 14: an approval needs the calculation")
    )
    work_out(client, "REQ-7001")
    other = record(client, "REQ-7001", payout=250.0)
    assert other.status_code == 422
    assert other.json()["detail"] == (
        "Claims manual section 14: the payout must be the calculation's $300.00"
    )


def test_a_complete_approval(client: TestClient):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    calculation = work_out(client, "REQ-7001").json()
    decision = record(client, "REQ-7001").json()["decision"]
    assert decision["calculation"] == {
        "lines": calculation["calculation"],
        "payout": 300.0,
    }
    assert decision["decided_by"] == MAKER
    assert decision["decided_at"]
    assert decision["manual_version"] == "2026.2"


@pytest.mark.parametrize("kind", ["decline", "refer"])
def test_a_decline_or_a_referral_holds_no_calculation(client: TestClient, kind: str):
    client.post("/api/requests/REQ-7001/start", headers=AGENT_HEADERS)
    decision = record(client, "REQ-7001", decision=kind, payout=0).json()["decision"]
    assert decision["calculation"] is None
    assert (decision["decided_by"], decision["manual_version"]) == (MAKER, "2026.2")


def test_the_record_holds_every_field_the_data_dictionary_names():
    dictionary = (CONTEXT / "data_dictionary.yaml").read_text(encoding="utf-8")
    [claim] = [
        r for r in yaml.safe_load(dictionary)["records"] if r["record"] == "claim"
    ]
    named = {
        field["field"].removeprefix("decision.")
        for field in claim["fields"]
        if field["field"].startswith("decision.")
    }
    assert named <= set(Decision.model_fields)
    assert {"calculation", "decided_by", "manual_version"} <= named
