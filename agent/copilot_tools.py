"""Synchronous Copilot tools scoped to the calling handler's permissions."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.store.base import BaseStore

from agent.copilot_memory import recall_handler_memories
from agent.direct import from_environment as direct_from_environment
from agent.gateway import from_environment as gateway_from_environment
from agent.routes import GatewayRefused, Route, Target

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_APPROVAL_LIMIT = 10000.0
SIGN_OFF_MARKER = "__sign_off_pause__"
MAX_WAIT_POLL_SECONDS = 20
_CLIENT_CACHE: dict[str, httpx.Client] = {}


@dataclass(frozen=True)
class CopilotContext:
    """Ephemeral per-turn runtime context; never stored in LangGraph checkpoints."""

    handler_token: str
    handler_id: str
    handler_name: str
    can_sign_off_over_limit: bool
    conversation_id: str
    turn_id: str
    backend_url: str = DEFAULT_BACKEND_URL
    context_route: Route | None = None
    memory_store: BaseStore | None = None
    http_client: httpx.Client | None = None
    step_sink: Callable[[dict[str, Any]], None] | None = None


class BackendRefused(Exception):
    """Raised when the Shoppie backend rejects a handler-scoped call."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def backend_base_url() -> str:
    """Resolve the Shoppie backend base URL from UNDERCOVER_API or PORT."""
    if url := os.getenv("UNDERCOVER_API"):
        return url.rstrip("/")
    if port := os.getenv("PORT"):
        return f"http://127.0.0.1:{port}"
    return DEFAULT_BACKEND_URL


def sanitize_value(value: Any, secret: str) -> Any:
    """Replace any occurrence of the handler bearer token in logs or steps."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [sanitize_value(item, secret) for item in value]
    if isinstance(value, dict):
        return {k: sanitize_value(v, secret) for k, v in value.items()}
    return value


def _get_http_client(ctx: CopilotContext) -> httpx.Client:
    if ctx.http_client is not None:
        return ctx.http_client
    base = ctx.backend_url.rstrip("/")
    existing = _CLIENT_CACHE.get(base)
    if existing is None or existing.is_closed:
        existing = httpx.Client(
            base_url=base,
            timeout=30,
            transport=httpx.HTTPTransport(retries=2),
        )
        _CLIENT_CACHE[base] = existing
    return existing


def handler_backend_call(
    ctx: CopilotContext,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call Shoppie's backend as the handler using only their ephemeral token."""
    normalized = path if path.startswith("/api") else f"/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {ctx.handler_token}"}
    client = _get_http_client(ctx)
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = client.request(method, normalized, **kwargs)
    if response.is_error:
        detail = response.text
        with contextlib.suppress(Exception):
            detail = str(response.json().get("detail", detail))
        raise BackendRefused(response.status_code, detail)
    return response.json()


def emit_step(ctx: CopilotContext, payload: dict[str, Any]) -> None:
    """Sanitize and record one progress step for the live Copilot sidebar."""
    clean = sanitize_value(payload, ctx.handler_token)
    if ctx.step_sink is not None:
        ctx.step_sink(clean)
        return
    if not ctx.conversation_id or not ctx.turn_id:
        return
    body = {
        "turn_id": ctx.turn_id,
        "kind": str(clean.get("kind") or "tool"),
        "tool_name": str(clean.get("tool_name") or ""),
        "summary": str(clean.get("summary") or "Completed step"),
        "detail": clean.get("detail") or {},
        "duration_ms": int(clean.get("duration_ms") or 0),
    }
    with contextlib.suppress(Exception):
        handler_backend_call(
            ctx,
            "POST",
            f"/api/copilot/conversations/{ctx.conversation_id}/steps",
            body,
        )


def _format_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _run_backend_tool(
    ctx: CopilotContext,
    tool_name: str,
    summary: str,
    fn: Callable[[], Any],
) -> str:
    started = time.monotonic()
    try:
        data = fn()
    except BackendRefused as err:
        elapsed = int((time.monotonic() - started) * 1000)
        emit_step(
            ctx,
            {
                "kind": "tool",
                "tool_name": tool_name,
                "summary": f"{summary} refused ({err.status_code}).",
                "detail": {"status": err.status_code, "error": err.detail},
                "duration_ms": elapsed,
            },
        )
        return f"Refused ({err.status_code}): {err.detail}"
    elapsed = int((time.monotonic() - started) * 1000)
    emit_step(
        ctx,
        {
            "kind": "tool",
            "tool_name": tool_name,
            "summary": summary,
            "detail": data if isinstance(data, dict) else {"result": data},
            "duration_ms": elapsed,
        },
    )
    return _format_json(data)


def _resolve_context_route(ctx: CopilotContext) -> Route:
    if ctx.context_route is not None:
        return ctx.context_route
    if gw := gateway_from_environment():
        return gw
    return direct_from_environment()


def _call_context_mcp(
    ctx: CopilotContext,
    tool_name: str,
    summary: str,
    inputs: dict[str, Any],
) -> str:
    started = time.monotonic()
    route = _resolve_context_route(ctx)
    try:
        # Context lookups stay strictly on Target.CONTEXT (undercover-context).
        result = route.call(Target.CONTEXT, tool_name, inputs)
    except (GatewayRefused, ToolException, BackendRefused, Exception) as err:
        elapsed = int((time.monotonic() - started) * 1000)
        emit_step(
            ctx,
            {
                "kind": "tool",
                "tool_name": tool_name,
                "summary": f"Context lookup {tool_name} failed.",
                "detail": {"error": str(err)},
                "duration_ms": elapsed,
            },
        )
        return f"Context lookup refused: {err}"
    elapsed = int((time.monotonic() - started) * 1000)
    detail = result if isinstance(result, dict) else {"result": result}
    emit_step(
        ctx,
        {
            "kind": "tool",
            "tool_name": tool_name,
            "summary": summary,
            "detail": detail,
            "duration_ms": elapsed,
        },
    )
    return _format_json(result)


@tool
def get_pilot_overview(runtime: ToolRuntime[CopilotContext]) -> str:
    """Get queue counts, pending sign-offs, runs, and success criteria."""
    ctx = runtime.context
    return _run_backend_tool(
        ctx,
        "get_pilot_overview",
        "Checked queue overview and pilot success criteria.",
        lambda: handler_backend_call(ctx, "GET", "/api/copilot/overview"),
    )


@tool
def list_requests(runtime: ToolRuntime[CopilotContext], status_filter: str = "") -> str:
    """List support requests, optionally filtering by queue status."""
    ctx = runtime.context

    def _fetch() -> list[dict[str, Any]]:
        rows = handler_backend_call(ctx, "GET", "/api/requests")
        wanted = status_filter.strip()
        if wanted:
            rows = [item for item in rows if item.get("status") == wanted]
        return [
            {
                "request_id": item["request_id"],
                "customer_id": item["customer_id"],
                "customer_name": item["customer_name"],
                "type": item["type"],
                "status": item["status"],
                "created_at": item["created_at"],
                "payout": (item.get("recommendation") or {}).get("payout"),
                "outcome": (item.get("recommendation") or {}).get("outcome"),
            }
            for item in rows
        ]

    label = status_filter.strip() or "all"
    return _run_backend_tool(
        ctx, "list_requests", f"Listed {label} support requests.", _fetch
    )


@tool
def get_request_details(request_id: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Get full details, order items, agent recommendation, review, and steps."""
    ctx = runtime.context
    clean_id = request_id.strip().upper()
    return _run_backend_tool(
        ctx,
        "get_request_details",
        f"Read request {clean_id}.",
        lambda: handler_backend_call(ctx, "GET", f"/api/requests/{clean_id}"),
    )


@tool
def get_customer_history(customer_id: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Get a customer's past Shoppie orders and support requests."""
    ctx = runtime.context
    clean_id = customer_id.strip().upper()
    return _run_backend_tool(
        ctx,
        "get_customer_history",
        f"Looked up customer {clean_id} history.",
        lambda: handler_backend_call(ctx, "GET", f"/api/copilot/customers/{clean_id}"),
    )


@tool
def get_pilot_settings(runtime: ToolRuntime[CopilotContext]) -> str:
    """Get Shoppie pilot settings including approval limit, targets, and cap."""
    ctx = runtime.context
    return _run_backend_tool(
        ctx,
        "get_pilot_settings",
        "Checked pilot settings and thresholds.",
        lambda: handler_backend_call(ctx, "GET", "/api/settings"),
    )


@tool
def search_context_documents(query: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Search Shoppie policy wording and the claims manual in the context layer."""
    return _call_context_mcp(
        runtime.context,
        "search_documents",
        f"Searched policies and manual for '{query}'.",
        {"query": query},
    )


@tool
def read_context_section(
    document: str, number: int, runtime: ToolRuntime[CopilotContext]
) -> str:
    """Read one numbered section of policy wording or the claims manual."""
    return _call_context_mcp(
        runtime.context,
        "read_section",
        f"Read {document} section {number}.",
        {"document": document, "number": number},
    )


@tool
def lookup_policy_term(term: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Look up a defined policy or support term in the context layer."""
    return _call_context_mcp(
        runtime.context,
        "lookup_term",
        f"Looked up term '{term}'.",
        {"term": term},
    )


@tool
def get_claim_rules_context(runtime: ToolRuntime[CopilotContext]) -> str:
    """Read operational claim rules in the context layer."""
    return _call_context_mcp(
        runtime.context,
        "get_claim_rules",
        "Checked support operational claim rules.",
        {},
    )


@tool
def get_plan_context(plan: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Read plan limits, deductibles, and add-ons in the context layer."""
    return _call_context_mcp(
        runtime.context,
        "get_plan",
        f"Read plan details for '{plan}'.",
        {"plan": plan},
    )


@tool
def search_reference_context(query: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Search external reference facts (weather, courier events, pricing)."""
    return _call_context_mcp(
        runtime.context,
        "search_reference_data",
        f"Searched external reference facts for '{query}'.",
        {"query": query},
    )


@tool
def describe_context_fields(record: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Describe fields on a Shoppie support record schema."""
    return _call_context_mcp(
        runtime.context,
        "describe_fields",
        f"Checked field schema for '{record}'.",
        {"record": record},
    )


@tool
def start_queue_run(runtime: ToolRuntime[CopilotContext], cap: int = 5) -> str:
    """Start the Shoppie agent run for up to `cap` waiting requests."""
    ctx = runtime.context
    bounded_cap = max(1, int(cap))
    return _run_backend_tool(
        ctx,
        "start_queue_run",
        f"Started queue run (cap {bounded_cap}).",
        lambda: handler_backend_call(ctx, "POST", "/api/runs", {"cap": bounded_cap}),
    )


@tool
def wait_for_queue_run(
    run_id: str,
    runtime: ToolRuntime[CopilotContext],
    max_wait_seconds: int = 12,
) -> str:
    """Wait briefly for a queue run and report current to-do progress honestly."""
    ctx = runtime.context
    deadline = time.monotonic() + max(
        1, min(int(max_wait_seconds), MAX_WAIT_POLL_SECONDS)
    )

    def _poll() -> dict[str, Any]:
        clean_run = run_id.strip().upper()
        latest = handler_backend_call(ctx, "GET", f"/api/runs/{clean_run}")
        while latest.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(2.0)
            latest = handler_backend_call(ctx, "GET", f"/api/runs/{clean_run}")
        return latest

    return _run_backend_tool(
        ctx,
        "wait_for_queue_run",
        f"Checked progress on run {run_id.strip().upper()}.",
        _poll,
    )


@tool
def start_quality_benchmark(
    runtime: ToolRuntime[CopilotContext],
    scope: Literal["full", "sample"] = "full",
) -> str:
    """Start a quality benchmark run over sample (5) or full Shoppie cases."""
    ctx = runtime.context
    return _run_backend_tool(
        ctx,
        "start_quality_benchmark",
        f"Started {scope} quality benchmark.",
        lambda: handler_backend_call(
            ctx, "POST", "/api/quality_runs", {"scope": scope}
        ),
    )


@tool
def get_quality_benchmark(
    runtime: ToolRuntime[CopilotContext], benchmark_id: str = ""
) -> str:
    """Get a specific quality benchmark or the latest benchmark status."""
    ctx = runtime.context
    clean_id = benchmark_id.strip().upper()
    path = f"/api/quality_runs/{clean_id}" if clean_id else "/api/quality_runs/current"
    return _run_backend_tool(
        ctx,
        "get_quality_benchmark",
        f"Checked benchmark {clean_id or 'latest'}.",
        lambda: handler_backend_call(ctx, "GET", path),
    )


@tool
def send_back_request(
    request_id: str, note: str, runtime: ToolRuntime[CopilotContext]
) -> str:
    """Send a recommended support request back to the agent with a required note."""
    ctx = runtime.context
    clean_id = request_id.strip().upper()
    clean_note = note.strip()
    return _run_backend_tool(
        ctx,
        "send_back_request",
        f"Sent {clean_id} back to the agent.",
        lambda: handler_backend_call(
            ctx,
            "POST",
            f"/api/requests/{clean_id}/send_back",
            {"note": clean_note, "sent_back_by": ctx.handler_name},
        ),
    )


@tool
def prepare_sign_off(
    request_id: str,
    note: str,
    outcome: Literal["approve", "decline", "request_information"],
    runtime: ToolRuntime[CopilotContext],
) -> str:
    """Validate handler permission and prepare a sign-off confirmation card."""
    ctx = runtime.context
    clean_id = request_id.strip().upper()
    started = time.monotonic()
    try:
        record = handler_backend_call(ctx, "GET", f"/api/requests/{clean_id}")
        settings = handler_backend_call(ctx, "GET", "/api/settings")
    except BackendRefused as err:
        elapsed = int((time.monotonic() - started) * 1000)
        emit_step(
            ctx,
            {
                "kind": "tool",
                "tool_name": "prepare_sign_off",
                "summary": f"Could not inspect {clean_id} for sign-off.",
                "detail": {"error": err.detail},
                "duration_ms": elapsed,
            },
        )
        return f"Refused ({err.status_code}): {err.detail}"

    if record.get("status") not in {"waiting_for_agent_review", "decided"}:
        status = record.get("status")
        return f"{clean_id} is currently '{status}' and cannot be signed off."
    rec = record.get("recommendation") or {}
    payout = rec.get("payout")
    if payout is None:
        payout = rec.get("refund_amount")
    limit = float(settings.get("approval_limit", DEFAULT_APPROVAL_LIMIT))
    if (
        outcome == "approve"
        and isinstance(payout, (int, float))
        and float(payout) > limit
        and not ctx.can_sign_off_over_limit
    ):
        elapsed = int((time.monotonic() - started) * 1000)
        reason = (
            f"{ctx.handler_name} cannot sign off {clean_id} because the "
            f"₹{float(payout):,.2f} payout exceeds the ₹{limit:,.2f} limit."
        )
        emit_step(
            ctx,
            {
                "kind": "tool",
                "tool_name": "prepare_sign_off",
                "summary": f"Denied over-limit sign-off for {clean_id}.",
                "detail": {"payout": payout, "limit": limit, "reason": reason},
                "duration_ms": elapsed,
            },
        )
        return f"Permission denied: {reason}"

    payload = {
        SIGN_OFF_MARKER: True,
        "request_id": clean_id,
        "customer_name": (record.get("customer") or {}).get("name", "Customer"),
        "payout": float(payout) if isinstance(payout, (int, float)) else 0.0,
        "recommendation": rec.get("outcome") or rec.get("decision") or outcome,
        "outcome": outcome,
        "note": note.strip() or f"Confirmed {outcome} by {ctx.handler_name}",
    }
    elapsed = int((time.monotonic() - started) * 1000)
    emit_step(
        ctx,
        {
            "kind": "confirmation",
            "tool_name": "prepare_sign_off",
            "summary": f"Prepared sign-off confirmation for {clean_id}.",
            "detail": {k: v for k, v in payload.items() if k != SIGN_OFF_MARKER},
            "duration_ms": elapsed,
        },
    )
    return json.dumps(payload)


@tool
def recall_handler_memory(query: str, runtime: ToolRuntime[CopilotContext]) -> str:
    """Search remembered preferences and facts for the signed-in handler only."""
    ctx = runtime.context
    started = time.monotonic()
    try:
        items = recall_handler_memories(ctx.memory_store, ctx.handler_id, query)
    except Exception as err:
        elapsed = int((time.monotonic() - started) * 1000)
        emit_step(
            ctx,
            {
                "kind": "tool",
                "tool_name": "recall_handler_memory",
                "summary": "Memory store unavailable.",
                "detail": {"error": str(err)},
                "duration_ms": elapsed,
            },
        )
        return "Handler memory unavailable right now."
    elapsed = int((time.monotonic() - started) * 1000)
    emit_step(
        ctx,
        {
            "kind": "tool",
            "tool_name": "recall_handler_memory",
            "summary": f"Checked handler memory ({len(items)} matches).",
            "detail": {"matches": items},
            "duration_ms": elapsed,
        },
    )
    return _format_json(items)


def execute_confirmed_review(
    ctx: CopilotContext,
    confirmation: dict[str, Any],
    approved: bool,
    note: str = "",
) -> dict[str, Any]:
    """Commit or cancel a sign-off after the handler responds to interrupt()."""
    request_id = str(confirmation["request_id"])
    if not approved:
        emit_step(
            ctx,
            {
                "kind": "confirmation",
                "tool_name": "review_request",
                "summary": f"Handler cancelled sign-off for {request_id}.",
                "detail": {"request_id": request_id, "approved": False},
                "duration_ms": 0,
            },
        )
        return {"cancelled": True, "request_id": request_id}

    payout = confirmation.get("payout")
    settings = handler_backend_call(ctx, "GET", "/api/settings")
    limit = float(settings.get("approval_limit", DEFAULT_APPROVAL_LIMIT))
    outcome = str(confirmation.get("outcome") or "approve")
    if (
        outcome == "approve"
        and isinstance(payout, (int, float))
        and float(payout) > limit
        and not ctx.can_sign_off_over_limit
    ):
        raise BackendRefused(
            403,
            f"{ctx.handler_name} cannot sign off payouts above ₹{limit:,.2f}.",
        )
    review_note = note.strip() or str(confirmation.get("note") or "Signed off")
    started = time.monotonic()
    updated = handler_backend_call(
        ctx,
        "POST",
        f"/api/requests/{request_id}/review",
        {
            "outcome": outcome,
            "notes": review_note,
            "reviewed_by": ctx.handler_name,
        },
    )
    elapsed = int((time.monotonic() - started) * 1000)
    emit_step(
        ctx,
        {
            "kind": "confirmation",
            "tool_name": "review_request",
            "summary": f"Signed off {request_id} ({outcome}).",
            "detail": {
                "request_id": request_id,
                "status": updated.get("status"),
                "outcome": outcome,
            },
            "duration_ms": elapsed,
        },
    )
    return updated


def build_copilot_tools() -> list[BaseTool]:
    """Return the full list of tools available to the Shoppie Copilot."""
    return [
        get_pilot_overview,
        list_requests,
        get_request_details,
        get_customer_history,
        get_pilot_settings,
        search_context_documents,
        read_context_section,
        lookup_policy_term,
        get_claim_rules_context,
        get_plan_context,
        search_reference_context,
        describe_context_fields,
        start_queue_run,
        wait_for_queue_run,
        start_quality_benchmark,
        get_quality_benchmark,
        send_back_request,
        prepare_sign_off,
        recall_handler_memory,
    ]
