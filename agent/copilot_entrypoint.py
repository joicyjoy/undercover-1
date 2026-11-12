"""Bedrock AgentCore entrypoint for the Shoppie Copilot runtime."""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import threading
from collections.abc import Callable
from typing import Any, Literal

import httpx
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from pydantic import BaseModel, Field

from agent.copilot_graph import (
    build_copilot_graph,
    resume_copilot_turn,
    run_copilot_turn,
)
from agent.copilot_memory import make_copilot_memory_store
from agent.copilot_tools import (
    CopilotContext,
    backend_base_url,
    emit_step,
    handler_backend_call,
    sanitize_value,
)

logger = logging.getLogger(__name__)
DEFAULT_LOCAL_PORT = 8081

app = BedrockAgentCoreApp()
_GRAPH_LOCK = threading.Lock()
_GRAPH_CACHE: dict[str, Any] = {}


class HealthPayload(BaseModel):
    """Health ping payload for the Copilot runtime."""

    action: Literal["health"] = "health"


class CopilotTurnPayload(BaseModel):
    """Asynchronous turn request dispatched when a handler sends a prompt."""

    action: Literal["copilot_turn"]
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    handler_token: str = Field(min_length=1)
    page_path: str = "/"
    selected_request_id: str = ""
    backend_url: str = ""


class CopilotResumePayload(BaseModel):
    """Asynchronous resume request after a sign-off confirmation interrupt."""

    action: Literal["copilot_resume"]
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    approved: bool
    handler_token: str = Field(min_length=1)
    note: str = ""
    backend_url: str = ""


def on_runtime() -> bool:
    """Return True when running inside AWS Bedrock AgentCore Runtime."""
    return bool(
        os.getenv("AWS_EXECUTION_ENV") or os.getenv("BEDROCK_AGENTCORE_RUNTIME")
    )


def _require_bearer_key(context: RequestContext) -> None:
    expected = os.getenv("AGENTCORE_RUNTIME_BEARER_TOKEN", "").strip()
    if not expected:
        return
    headers = getattr(context, "request_headers", None) or {}
    auth = ""
    for key, val in headers.items():
        if str(key).casefold() == "authorization":
            auth = str(val)
            break
    scheme, _, token = auth.partition(" ")
    if scheme.casefold() != "bearer" or not hmac.compare_digest(
        token.strip(), expected
    ):
        raise PermissionError("Unauthorized runtime caller")


def get_copilot_graph() -> Any:
    """Return the compiled Copilot graph cached per process."""
    with _GRAPH_LOCK:
        if "graph" not in _GRAPH_CACHE:
            _GRAPH_CACHE["graph"] = build_copilot_graph()
        return _GRAPH_CACHE["graph"]


def _verify_handler_me(ctx: CopilotContext) -> dict[str, Any]:
    data = handler_backend_call(ctx, "GET", "/api/copilot/me")
    handler = data.get("handler") or {}
    if not handler.get("handler_id"):
        raise RuntimeError("Backend did not return a handler identity")
    return dict(handler)


def _complete_turn(ctx: CopilotContext, outcome: dict[str, Any]) -> None:
    body = {
        "status": str(outcome.get("status") or "failed"),
        "assistant_message": str(outcome.get("assistant_message") or ""),
        "confirmation": outcome.get("confirmation") or {},
        "error": str(sanitize_value(outcome.get("error") or "", ctx.handler_token)),
    }
    handler_backend_call(
        ctx,
        "POST",
        f"/api/copilot/conversations/{ctx.conversation_id}/turns/{ctx.turn_id}/complete",
        body,
    )


def process_copilot_task(
    payload: CopilotTurnPayload | CopilotResumePayload,
    *,
    graph_factory: Callable[[], Any] = get_copilot_graph,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Execute one Copilot turn or confirmation resume synchronously."""
    resolved_url = (payload.backend_url or backend_base_url()).rstrip("/")
    memory_store = None
    with contextlib.suppress(Exception):
        memory_store = make_copilot_memory_store()
    bootstrap_ctx = CopilotContext(
        handler_token=payload.handler_token,
        handler_id="",
        handler_name="",
        can_sign_off_over_limit=False,
        conversation_id=payload.conversation_id,
        turn_id=payload.turn_id,
        backend_url=resolved_url,
        memory_store=memory_store,
        http_client=http_client,
    )
    try:
        handler_info = _verify_handler_me(bootstrap_ctx)
        ctx = CopilotContext(
            handler_token=payload.handler_token,
            handler_id=str(handler_info["handler_id"]),
            handler_name=str(handler_info.get("name") or handler_info["handler_id"]),
            can_sign_off_over_limit=bool(
                handler_info.get("can_sign_off_over_limit", False)
            ),
            conversation_id=payload.conversation_id,
            turn_id=payload.turn_id,
            backend_url=resolved_url,
            memory_store=memory_store,
            http_client=http_client,
        )
        graph = graph_factory()
        if isinstance(payload, CopilotTurnPayload):
            outcome = run_copilot_turn(
                graph,
                ctx,
                {
                    "message": payload.message,
                    "page_path": payload.page_path,
                    "selected_request_id": payload.selected_request_id,
                },
            )
        else:
            outcome = resume_copilot_turn(
                graph, ctx, approved=payload.approved, note=payload.note
            )
        _complete_turn(ctx, outcome)
        return outcome
    except Exception as err:
        safe_msg = str(sanitize_value(str(err), payload.handler_token))
        logger.warning(
            "Copilot task failed for %s/%s: %s",
            payload.conversation_id,
            payload.turn_id,
            safe_msg,
        )
        emit_step(
            bootstrap_ctx,
            {
                "kind": "error",
                "tool_name": "",
                "summary": "Copilot hit an error while working this turn.",
                "detail": {"error": safe_msg},
                "duration_ms": 0,
            },
        )
        failure = {"status": "failed", "error": safe_msg}
        with contextlib.suppress(Exception):
            _complete_turn(bootstrap_ctx, failure)
        return failure


def _run_background(
    task_name: str, payload: CopilotTurnPayload | CopilotResumePayload
) -> None:
    task_id = app.add_async_task(task_name)

    def _worker() -> None:
        try:
            process_copilot_task(payload)
        finally:
            app.complete_async_task(task_id)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


@app.entrypoint
def invoke(payload: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    """Handle health checks, new Copilot turns, and sign-off confirmation resumes."""
    _require_bearer_key(context)
    action = str(payload.get("action") or payload.get("operation") or "")
    if action == "health":
        HealthPayload.model_validate({"action": "health"})
        return {"status": "ok"}
    if action == "copilot_turn":
        turn_payload = CopilotTurnPayload.model_validate(
            {**payload, "action": "copilot_turn"}
        )
        _run_background("copilot_turn", turn_payload)
        return {
            "status": "accepted",
            "conversation_id": turn_payload.conversation_id,
            "turn_id": turn_payload.turn_id,
        }
    if action == "copilot_resume":
        resume_payload = CopilotResumePayload.model_validate(
            {**payload, "action": "copilot_resume"}
        )
        _run_background("copilot_resume", resume_payload)
        return {
            "status": "accepted",
            "conversation_id": resume_payload.conversation_id,
            "turn_id": resume_payload.turn_id,
        }
    return {"status": "rejected", "error": f"Unknown action: {action}"}


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", str(DEFAULT_LOCAL_PORT))))
