"""Phase 2 tests for handler-scoped Copilot tools and Gateway routing."""

import json
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi.testclient import TestClient
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime

from agent.copilot_tools import (
    SIGN_OFF_MARKER,
    CopilotContext,
    emit_step,
    execute_confirmed_review,
    get_customer_history,
    get_pilot_overview,
    get_request_details,
    list_requests,
    prepare_sign_off,
    recall_handler_memory,
    search_context_documents,
    send_back_request,
    start_quality_benchmark,
)
from agent.routes import Target
from agent.tests.memory_stand_in import MemoryStandIn
from backend.handlers import HANDLERS, create_handler_token
from backend.main import app
from backend.store import Store


@dataclass
class _StubContextRoute:
    calls: list[tuple[Target, str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    def name(self, target: Target, operation: str) -> str:
        return f"{target.value}___{operation}"

    def call(self, target: Target, operation: str, inputs: dict[str, Any]) -> Any:
        if target != Target.CONTEXT:
            raise AssertionError(f"Unexpected target for Copilot: {target}")
        if self.fail:
            raise RuntimeError("Context Gateway unreachable")
        self.calls.append((target, operation, inputs))
        return {"matches": [{"document": "manual", "section": 3, "excerpt": "Rule"}]}


class _FastApiHttpxShim:
    """Adapter presenting httpx.Client-like .request() over FastAPI TestClient."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._client.request(method, url, **kwargs)


def _call(bound_tool: BaseTool, **kwargs: Any) -> str:
    fn = cast(StructuredTool, bound_tool).func
    assert fn is not None
    return str(fn(**kwargs))


def _make_runtime(ctx: CopilotContext) -> ToolRuntime[CopilotContext]:
    return ToolRuntime(
        state={},
        context=ctx,
        config={},
        stream_writer=lambda _x: None,
        tool_call_id="tc-test",
        store=ctx.memory_store,
    )


def test_tools_use_handler_bearer_token_and_never_api_key(tmp_path, monkeypatch):
    db = tmp_path / "tools.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    monkeypatch.setenv("UNDERCOVER_API_KEY", "secret-agent-key-not-for-copilot")
    steps: list[dict[str, Any]] = []
    dana_token = create_handler_token(HANDLERS["HND-01"])
    route = _StubContextRoute()

    with TestClient(app) as client:
        ctx = CopilotContext(
            handler_token=dana_token,
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-1",
            turn_id="TURN-1",
            http_client=_FastApiHttpxShim(client),  # type: ignore[arg-type]
            context_route=route,
            step_sink=steps.append,
        )
        rt = _make_runtime(ctx)
        overview = json.loads(_call(get_pilot_overview, runtime=rt))
        reqs = json.loads(_call(list_requests, runtime=rt))
        detail = json.loads(
            _call(get_request_details, request_id="REQ-7001", runtime=rt)
        )
        cust = json.loads(
            _call(
                get_customer_history,
                customer_id=detail["customer"]["customer_id"],
                runtime=rt,
            )
        )
        bench = json.loads(_call(start_quality_benchmark, runtime=rt, scope="sample"))
        docs = json.loads(
            _call(search_context_documents, query="damaged item", runtime=rt)
        )

    assert overview["total_requests"] == 9
    assert len(reqs) == 9
    assert detail["request"]["request_id"] == "REQ-7001"
    assert cust["customer_id"] == detail["customer"]["customer_id"]
    assert bench["total_cases"] == 5
    assert docs["matches"][0]["section"] == 3
    assert route.calls == [
        (Target.CONTEXT, "search_documents", {"query": "damaged item"})
    ]
    assert len(steps) == 6
    assert dana_token not in json.dumps(steps)


def test_step_redacts_handler_token():
    recorded: list[dict[str, Any]] = []
    bearer = create_handler_token(HANDLERS["HND-01"])
    ctx = CopilotContext(
        handler_token=bearer,
        handler_id="HND-01",
        handler_name="Dana Whitfield",
        can_sign_off_over_limit=True,
        conversation_id="CONV-1",
        turn_id="TURN-1",
        step_sink=recorded.append,
    )
    emit_step(
        ctx,
        {
            "kind": "tool",
            "tool_name": "test",
            "summary": f"Header Bearer {bearer}",
            "detail": {"raw": bearer},
        },
    )
    assert bearer not in json.dumps(recorded)
    assert "[REDACTED]" in recorded[0]["summary"]


def test_permission_block_before_pause_and_send_back_direct(tmp_path, monkeypatch):
    db = tmp_path / "perm_tools.sqlite3"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(db))
    store = Store(db)
    store.initialize()
    assert store.start_request("REQ-7001") == 1
    assert store.save_recommendation(
        "REQ-7001",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 14000.0,
            "refund_amount": 14000.0,
            "section": "Section 1",
            "reason": "High value item",
            "decided_by": "Shoppie Agent",
        },
    )
    assert store.start_request("REQ-7002") == 1
    assert store.save_recommendation(
        "REQ-7002",
        {
            "decision": "approve",
            "outcome": "approve",
            "payout": 800.0,
            "refund_amount": 800.0,
            "section": "Section 2",
            "reason": "Damaged mug",
            "decided_by": "Shoppie Agent",
        },
    )

    marcus_token = create_handler_token(HANDLERS["HND-02"])
    dana_token = create_handler_token(HANDLERS["HND-01"])
    with TestClient(app) as client:
        shim = _FastApiHttpxShim(client)
        marcus_ctx = CopilotContext(
            handler_token=marcus_token,
            handler_id="HND-02",
            handler_name="Marcus Oyelaran",
            can_sign_off_over_limit=False,
            conversation_id="CONV-M",
            turn_id="TURN-M",
            http_client=shim,  # type: ignore[arg-type]
        )
        denied = _call(
            prepare_sign_off,
            request_id="REQ-7001",
            note="Approving over limit",
            outcome="approve",
            runtime=_make_runtime(marcus_ctx),
        )
        sent_back = json.loads(
            _call(
                send_back_request,
                request_id="REQ-7002",
                note="Please check unboxing photo",
                runtime=_make_runtime(marcus_ctx),
            )
        )

        dana_ctx = CopilotContext(
            handler_token=dana_token,
            handler_id="HND-01",
            handler_name="Dana Whitfield",
            can_sign_off_over_limit=True,
            conversation_id="CONV-D",
            turn_id="TURN-D",
            http_client=shim,  # type: ignore[arg-type]
        )
        prepared = json.loads(
            _call(
                prepare_sign_off,
                request_id="REQ-7001",
                note="Approved by lead",
                outcome="approve",
                runtime=_make_runtime(dana_ctx),
            )
        )
        reviewed = execute_confirmed_review(dana_ctx, prepared, approved=True)

    assert denied.startswith("Permission denied:")
    assert sent_back["status"] == "sent_back"
    assert prepared[SIGN_OFF_MARKER] is True
    assert prepared["payout"] == 14000.0
    assert reviewed["status"] == "decided"
    assert reviewed["review"]["reviewed_by"] == "Dana Whitfield"


def test_context_failure_and_memory_fallback():
    route = _StubContextRoute(fail=True)
    stand_in = MemoryStandIn(reachable=False)
    bearer = create_handler_token(HANDLERS["HND-01"])
    ctx = CopilotContext(
        handler_token=bearer,
        handler_id="HND-01",
        handler_name="Dana Whitfield",
        can_sign_off_over_limit=True,
        conversation_id="CONV-1",
        turn_id="TURN-1",
        context_route=route,
        memory_store=stand_in,
    )
    rt = _make_runtime(ctx)
    assert (
        "refused" in _call(search_context_documents, query="policy", runtime=rt).lower()
    )
    assert (
        "unavailable" in _call(recall_handler_memory, query="style", runtime=rt).lower()
    )
