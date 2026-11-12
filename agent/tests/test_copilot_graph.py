"""Phase 3 tests for the hand-written Copilot StateGraph and interrupt resume."""

import json
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.copilot_graph import (
    build_copilot_graph,
    resume_copilot_turn,
    run_copilot_turn,
)
from agent.copilot_tools import CopilotContext
from agent.tests.memory_stand_in import MemoryStandIn
from agent.tests.stubs import StubModel, call
from backend.handlers import HANDLERS, create_handler_token
from backend.main import app
from backend.store import Store


class _FastApiHttpxShim:
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._client.request(method, url, **kwargs)


def test_graph_tool_loop_and_memory_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "loop.sqlite3"))
    steps: list[dict[str, Any]] = []
    mem = MemoryStandIn(
        memories=[("/handlers/HND-01/preferences/1", "Use concise INR bullets.")]
    )
    llm = StubModel(
        messages=iter(
            [
                call("get_pilot_settings"),
                AIMessage(content="Approval limit is ₹10,000.00."),
            ]
        )
    )

    with TestClient(app) as client:
        ctx = CopilotContext(
            handler_token=create_handler_token(HANDLERS["HND-01"]),
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-LOOP",
            turn_id="TURN-1",
            http_client=_FastApiHttpxShim(client),  # type: ignore[arg-type]
            memory_store=mem,
            step_sink=steps.append,
        )
        graph = build_copilot_graph(
            chat_model=llm,
            checkpointer=InMemorySaver(),
            memory_store=mem,
        )
        outcome = run_copilot_turn(
            graph,
            ctx,
            {"message": "What is our approval limit?", "page_path": "/"},
        )

    assert outcome == {
        "status": "done",
        "assistant_message": "Approval limit is ₹10,000.00.",
    }
    assert len(mem.events) == 2
    assert any(step["tool_name"] == "get_pilot_settings" for step in steps)


def test_graph_sign_off_interrupt_resume_across_instances_and_no_token_in_checkpoint(
    tmp_path, monkeypatch
):
    db = tmp_path / "graph_resume.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    store = Store(db)
    store.initialize()
    assert store.start_request("REQ-7001") == 1
    assert store.save_recommendation(
        "REQ-7001",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 12500.0,
            "refund_amount": 12500.0,
            "section": "Section 1",
            "reason": "Escalated payout",
            "decided_by": "Shoppie Agent",
        },
    )

    shared_cp = InMemorySaver()
    first_token = create_handler_token(HANDLERS["HND-01"])
    second_token = create_handler_token(HANDLERS["HND-01"])

    llm_first = StubModel(
        messages=iter(
            [
                call(
                    "prepare_sign_off",
                    {
                        "request_id": "REQ-7001",
                        "note": "Lead sign-off verified",
                        "outcome": "approve",
                    },
                )
            ]
        )
    )
    llm_second = StubModel(
        messages=iter([AIMessage(content="Signed off REQ-7001 for ₹12,500.00.")])
    )

    with TestClient(app) as client:
        shim = _FastApiHttpxShim(client)
        graph_one = build_copilot_graph(chat_model=llm_first, checkpointer=shared_cp)
        ctx_one = CopilotContext(
            handler_token=first_token,
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-INT-1",
            turn_id="TURN-1",
            http_client=shim,  # type: ignore[arg-type]
        )
        paused = run_copilot_turn(
            graph_one,
            ctx_one,
            {"message": "Please sign off REQ-7001", "page_path": "/requests/REQ-7001"},
        )

        # Ensure handler bearer token is absent from persisted checkpoint state.
        snapshot = graph_one.get_state({"configurable": {"thread_id": "CONV-INT-1"}})
        dumped_state = json.dumps(snapshot.values, default=str)
        assert first_token not in dumped_state

        # Resume on a fresh compiled graph using a freshly minted ephemeral token.
        graph_two = build_copilot_graph(chat_model=llm_second, checkpointer=shared_cp)
        ctx_two = CopilotContext(
            handler_token=second_token,
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-INT-1",
            turn_id="TURN-1",
            http_client=shim,  # type: ignore[arg-type]
        )
        finished = resume_copilot_turn(
            graph_two, ctx_two, approved=True, note="Looks good"
        )
        req_after = store.get_request("REQ-7001")

    assert paused["status"] == "waiting_confirmation"
    assert paused["confirmation"]["request_id"] == "REQ-7001"
    assert paused["confirmation"]["payout"] == 12500.0
    assert finished == {
        "status": "done",
        "assistant_message": "Signed off REQ-7001 for ₹12,500.00.",
    }
    assert req_after is not None
    assert req_after["status"] == "decided"
    assert req_after["review"]["reviewed_by"] == "Dana Whitfield"


def test_graph_cancel_confirmation_keeps_request_waiting_review(tmp_path, monkeypatch):
    db = tmp_path / "graph_cancel.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    store = Store(db)
    store.initialize()
    assert store.start_request("REQ-7002") == 1
    assert store.save_recommendation(
        "REQ-7002",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 900.0,
            "refund_amount": 900.0,
            "section": "Section 2",
            "reason": "Damaged mug",
            "decided_by": "Shoppie Agent",
        },
    )

    shared_cp = InMemorySaver()
    llm = StubModel(
        messages=iter(
            [
                call(
                    "prepare_sign_off",
                    {
                        "request_id": "REQ-7002",
                        "note": "Confirm mug refund",
                        "outcome": "approve",
                    },
                ),
                AIMessage(content="Cancelled sign-off for REQ-7002 as requested."),
            ]
        )
    )
    token = create_handler_token(HANDLERS["HND-01"])
    with TestClient(app) as client:
        ctx = CopilotContext(
            handler_token=token,
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-CANCEL",
            turn_id="TURN-1",
            http_client=_FastApiHttpxShim(client),  # type: ignore[arg-type]
        )
        graph = build_copilot_graph(chat_model=llm, checkpointer=shared_cp)
        paused = run_copilot_turn(
            graph, ctx, {"message": "Approve REQ-7002", "page_path": "/"}
        )
        resumed = resume_copilot_turn(graph, ctx, approved=False)
        req_after = store.get_request("REQ-7002")

    assert paused["status"] == "waiting_confirmation"
    assert resumed["status"] == "done"
    assert "Cancelled sign-off" in resumed["assistant_message"]
    assert req_after is not None
    assert req_after["status"] == "waiting_for_agent_review"
