"""Phase 3 tests for the Copilot AgentCore entrypoint and runtime launcher."""

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import copilot_entrypoint
from agent.copilot_entrypoint import (
    CopilotResumePayload,
    CopilotTurnPayload,
    invoke,
    process_copilot_task,
)
from agent.copilot_graph import build_copilot_graph
from agent.tests.stubs import StubModel, call
from backend.handlers import HANDLERS, create_handler_token
from backend.main import app
from backend.store import Store


class _FastApiHttpxShim:
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._client.request(method, url, **kwargs)


def test_entrypoint_health_and_bearer_guard(monkeypatch):
    monkeypatch.delenv("AGENTCORE_RUNTIME_BEARER_TOKEN", raising=False)
    ctx = SimpleNamespace(request_headers={})
    assert invoke({"action": "health"}, ctx) == {"status": "ok"}  # type: ignore[arg-type]

    monkeypatch.setenv("AGENTCORE_RUNTIME_BEARER_TOKEN", "runtime-shared-pass")
    with pytest.raises(PermissionError):
        invoke({"action": "health"}, ctx)  # type: ignore[arg-type]
    ok_ctx = SimpleNamespace(
        request_headers={"Authorization": "Bearer runtime-shared-pass"}
    )
    assert invoke({"action": "health"}, ok_ctx) == {"status": "ok"}  # type: ignore[arg-type]


def test_entrypoint_process_turn_pause_and_resume_with_backend(tmp_path, monkeypatch):
    db = tmp_path / "entry.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    store = Store(db)
    store.initialize()
    assert store.start_request("REQ-7001") == 1
    assert store.save_recommendation(
        "REQ-7001",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 1500.0,
            "refund_amount": 1500.0,
            "section": "Section 1",
            "reason": "Covered return",
            "decided_by": "Shoppie Agent",
        },
    )

    conv = store.create_conversation("HND-01", "Sign-off check")
    turn = store.start_copilot_turn(conv["conversation_id"], "HND-01", "Approve 7001")
    assert turn is not None
    dana_token = create_handler_token(HANDLERS["HND-01"])
    shared_cp = InMemorySaver()
    llm = StubModel(
        messages=iter(
            [
                call(
                    "prepare_sign_off",
                    {
                        "request_id": "REQ-7001",
                        "note": "Checked evidence",
                        "outcome": "approve",
                    },
                ),
                AIMessage(content="Confirmed sign-off for REQ-7001."),
            ]
        )
    )
    graph = build_copilot_graph(chat_model=llm, checkpointer=shared_cp)

    with TestClient(app) as client:
        shim = _FastApiHttpxShim(client)
        paused = process_copilot_task(
            CopilotTurnPayload(
                action="copilot_turn",
                conversation_id=conv["conversation_id"],
                turn_id=turn["turn_id"],
                message="Please sign off REQ-7001",
                handler_token=dana_token,
            ),
            graph_factory=lambda: graph,
            http_client=shim,  # type: ignore[arg-type]
        )
        mid_conv = client.get(
            f"/api/copilot/conversations/{conv['conversation_id']}",
            headers={"Authorization": f"Bearer {dana_token}"},
        ).json()
        confirmed = client.post(
            f"/api/copilot/conversations/{conv['conversation_id']}/turns/{turn['turn_id']}/confirm",
            json={"approved": True, "note": "Go ahead"},
            headers={"Authorization": f"Bearer {dana_token}"},
        )
        resumed = process_copilot_task(
            CopilotResumePayload(
                action="copilot_resume",
                conversation_id=conv["conversation_id"],
                turn_id=turn["turn_id"],
                approved=True,
                note="Go ahead",
                handler_token=dana_token,
            ),
            graph_factory=lambda: graph,
            http_client=shim,  # type: ignore[arg-type]
        )
        final_conv = client.get(
            f"/api/copilot/conversations/{conv['conversation_id']}",
            headers={"Authorization": f"Bearer {dana_token}"},
        ).json()

    assert paused["status"] == "waiting_confirmation"
    assert mid_conv["status"] == "waiting_confirmation"
    assert confirmed.status_code == 200
    assert resumed["status"] == "done"
    assert final_conv["status"] == "done"
    assert (
        final_conv["turns"][0]["assistant_message"]
        == "Confirmed sign-off for REQ-7001."
    )


def test_entrypoint_records_sanitized_failure_when_graph_raises(tmp_path, monkeypatch):
    db = tmp_path / "entry_fail.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    store = Store(db)
    store.initialize()
    conv = store.create_conversation("HND-01", "Failure test")
    turn = store.start_copilot_turn(conv["conversation_id"], "HND-01", "Trigger boom")
    assert turn is not None
    dana_token = create_handler_token(HANDLERS["HND-01"])

    def _broken_graph() -> Any:
        raise RuntimeError(f"Crash leaking {dana_token}")

    with TestClient(app) as client:
        shim = _FastApiHttpxShim(client)
        outcome = process_copilot_task(
            CopilotTurnPayload(
                action="copilot_turn",
                conversation_id=conv["conversation_id"],
                turn_id=turn["turn_id"],
                message="Trigger boom",
                handler_token=dana_token,
            ),
            graph_factory=_broken_graph,
            http_client=shim,  # type: ignore[arg-type]
        )
        updated = store.get_conversation(conv["conversation_id"])

    assert outcome["status"] == "failed"
    assert dana_token not in outcome["error"]
    assert updated is not None
    assert updated["status"] == "failed"
    assert dana_token not in (updated["turns"][0]["error"] or "")


def test_runtime_copilot_main_imports_cleanly():
    mod = importlib.import_module("runtime.copilot_main")
    assert mod is not None
    assert copilot_entrypoint.app is not None
