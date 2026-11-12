"""Hand-written synchronous LangGraph StateGraph for the Shoppie Copilot."""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, interrupt
from pydantic import SecretStr
from typing_extensions import TypedDict

from agent.copilot_memory import (
    UNREACHABLE,
    make_copilot_memory_store,
    recall_handler_memories,
    save_handler_turn_memory,
)
from agent.copilot_tools import (
    SIGN_OFF_MARKER,
    CopilotContext,
    build_copilot_tools,
    emit_step,
    execute_confirmed_review,
    sanitize_value,
)
from agent.main import call_with_retry, get_checkpointer

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_COPILOT_MODEL = "deepseek/deepseek-v4.1-flash"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class CopilotState(TypedDict):
    """Persistent conversation state stored cleanly without bearer credentials."""

    messages: Annotated[list[AnyMessage], add_messages]
    handler_id: str
    handler_name: str
    can_sign_off_over_limit: bool
    page_path: str
    selected_request_id: str
    latest_user_prompt: str
    recalled_memories: list[dict[str, str]]


def default_copilot_model() -> BaseChatModel:
    """Build the Copilot LLM from COPILOT_MODEL, MODEL_NAME, or default."""
    model_name = (
        os.getenv("COPILOT_MODEL", "").strip()
        or os.getenv("MODEL_NAME", "").strip()
        or DEFAULT_COPILOT_MODEL
    )
    return ChatOpenAI(
        model=model_name,
        base_url=OPENROUTER_BASE_URL,
        api_key=SecretStr(os.environ["OPENROUTER_API_KEY"]),
        timeout=120,
    )


def _system_prompt(state: CopilotState) -> str:
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    permission = (
        "You CAN sign off payouts above ₹10,000.00 after handler confirmation."
        if state.get("can_sign_off_over_limit")
        else (
            "You CANNOT sign off payouts above ₹10,000.00 "
            "(over-limit requires a Senior Support Lead)."
        )
    )
    selected = state.get("selected_request_id") or "None"
    memories = state.get("recalled_memories") or []
    memory_lines = (
        "\n".join(f"- [{m['kind']}] {m['content']}" for m in memories)
        if memories
        else "- None recorded for this query."
    )
    page = state.get("page_path") or "/"
    return (
        "You are the Shoppie Copilot, assisting Shoppie customer-support handlers.\n"
        f"Current time: {now_ist}\n"
        f"Handler: {state.get('handler_name')} ({state.get('handler_id')})\n"
        f"Authority: {permission}\n"
        f"Dashboard page: {page} | Selected request: {selected}\n\n"
        "Remembered handler preferences and facts:\n"
        f"{memory_lines}\n\n"
        "Rules:\n"
        "1. Always verify queue counts, requests, policies, customers, benchmarks, and "
        "criteria via tools before stating numbers or facts. Never invent metrics.\n"
        "2. All currency amounts are Indian Rupees (₹) and timestamps are IST.\n"
        "3. Never read or reference ground-truth evaluation labels.\n"
        "4. Use send_back_request immediately when asked; never call it without "
        "a note.\n"
        "5. Never sign off on a request directly: call prepare_sign_off so the "
        "handler confirms explicitly first, and report if permission is denied.\n"
        "6. When starting a run with start_queue_run, check progress with "
        "wait_for_queue_run and report what finished vs what is still running.\n"
        "7. Reference request IDs cleanly (such as REQ-7001) so handlers can click "
        "them.\n"
        "8. Never use exclamation marks in responses."
    )


def _extract_sign_off_payload(messages: list[AnyMessage]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            break
        if isinstance(msg, ToolMessage):
            with contextlib.suppress(Exception):
                parsed = json.loads(str(msg.content))
                if isinstance(parsed, dict) and parsed.get(SIGN_OFF_MARKER):
                    return {k: v for k, v in parsed.items() if k != SIGN_OFF_MARKER}
    return None


def _text_of_message(msg: AnyMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        ]
        return "".join(parts).strip()
    return str(content)


def _confirm_sign_off_node(
    state: CopilotState, runtime: Runtime[CopilotContext]
) -> dict[str, Any]:
    ctx = runtime.context
    confirmation = _extract_sign_off_payload(state["messages"])
    if confirmation is None:
        return {
            "messages": [
                HumanMessage(
                    content="[Confirmation result]: No pending sign-off found."
                )
            ]
        }
    decision = interrupt(confirmation)
    approved = bool(isinstance(decision, dict) and decision.get("approved"))
    note = str(decision.get("note") or "") if isinstance(decision, dict) else ""
    try:
        outcome = execute_confirmed_review(ctx, confirmation, approved, note=note)
    except Exception as err:
        safe_err = sanitize_value(str(err), ctx.handler_token)
        return {
            "messages": [
                HumanMessage(
                    content=f"[Confirmation result]: Sign-off failed: {safe_err}"
                )
            ]
        }
    req_id = confirmation["request_id"]
    if not approved:
        msg = f"[Confirmation result]: Handler cancelled sign-off for {req_id}."
    else:
        msg = (
            f"[Confirmation result]: Signed off {req_id} "
            f"(status: {outcome.get('status', 'decided')})."
        )
    return {"messages": [HumanMessage(content=msg)]}


def _after_model(state: CopilotState) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "__end__"


def _after_tools(state: CopilotState) -> Literal["confirm_sign_off", "model"]:
    if _extract_sign_off_payload(state["messages"]) is not None:
        return "confirm_sign_off"
    return "model"


def build_copilot_graph(
    chat_model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    memory_store: BaseStore | None = None,
) -> Any:
    """Compile the manual LangGraph StateGraph for the Shoppie Copilot."""
    tools = build_copilot_tools()
    llm = chat_model or default_copilot_model()
    bound_model = llm.bind_tools(tools) if hasattr(llm, "bind_tools") else llm

    def load_memory(
        state: CopilotState, runtime: Runtime[CopilotContext]
    ) -> dict[str, Any]:
        ctx = runtime.context
        store = ctx.memory_store or memory_store
        prompt = state.get("latest_user_prompt", "")
        recalled: list[dict[str, str]] = []
        if store is not None and prompt:
            with contextlib.suppress(*UNREACHABLE):
                recalled = recall_handler_memories(store, ctx.handler_id, prompt)
        return {"recalled_memories": recalled}

    def model_node(
        state: CopilotState, runtime: Runtime[CopilotContext]
    ) -> dict[str, Any]:
        ctx = runtime.context
        sys_msg = SystemMessage(content=_system_prompt(state))
        response = call_with_retry(bound_model.invoke, [sys_msg, *state["messages"]])
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            names = ", ".join(str(tc.get("name", "tool")) for tc in tool_calls)
            emit_step(
                ctx,
                {
                    "kind": "thinking",
                    "tool_name": "",
                    "summary": f"Selected tools: {names}.",
                    "detail": {"tools": names},
                    "duration_ms": 0,
                },
            )
        return {"messages": [response]}

    builder: StateGraph[CopilotState, CopilotContext, CopilotState, CopilotState] = (
        StateGraph(CopilotState, context_schema=CopilotContext)
    )
    builder.add_node("load_memory", load_memory)
    builder.add_node("model", model_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("confirm_sign_off", _confirm_sign_off_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "model")
    builder.add_conditional_edges(
        "model", _after_model, {"tools": "tools", "__end__": END}
    )
    builder.add_conditional_edges(
        "tools",
        _after_tools,
        {"confirm_sign_off": "confirm_sign_off", "model": "model"},
    )
    builder.add_edge("confirm_sign_off", "model")

    resolved_cp = checkpointer or get_checkpointer()
    resolved_store = (
        memory_store if memory_store is not None else make_copilot_memory_store()
    )
    return builder.compile(checkpointer=resolved_cp, store=resolved_store)


def _run_config(conversation_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": conversation_id}}


def _maybe_save_memory(ctx: CopilotContext, user_text: str, answer_text: str) -> None:
    store = ctx.memory_store
    if store is None or not user_text.strip() or not answer_text.strip():
        return
    with contextlib.suppress(Exception):
        save_handler_turn_memory(
            store,
            ctx.handler_id,
            ctx.conversation_id,
            [
                HumanMessage(content=sanitize_value(user_text, ctx.handler_token)),
                AIMessage(content=sanitize_value(answer_text, ctx.handler_token)),
            ],
        )


def _finalize_graph_result(
    ctx: CopilotContext, result: dict[str, Any], user_text: str
) -> dict[str, Any]:
    interrupts = result.get("__interrupt__")
    if interrupts:
        confirmation = dict(interrupts[0].value)
        return {"status": "waiting_confirmation", "confirmation": confirmation}
    messages = result.get("messages") or []
    answer = _text_of_message(messages[-1]) if messages else ""
    clean_answer = str(sanitize_value(answer, ctx.handler_token)).strip()
    _maybe_save_memory(ctx, user_text, clean_answer)
    return {"status": "done", "assistant_message": clean_answer}


def run_copilot_turn(
    graph: Any,
    ctx: CopilotContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run one Copilot user message synchronously until reply or interrupt pause."""
    message = str(payload["message"])
    initial: dict[str, Any] = {
        "messages": [HumanMessage(content=message)],
        "handler_id": ctx.handler_id,
        "handler_name": ctx.handler_name,
        "can_sign_off_over_limit": ctx.can_sign_off_over_limit,
        "page_path": str(payload.get("page_path") or "/"),
        "selected_request_id": str(payload.get("selected_request_id") or ""),
        "latest_user_prompt": message,
        "recalled_memories": [],
    }
    result = graph.invoke(initial, config=_run_config(ctx.conversation_id), context=ctx)
    return _finalize_graph_result(ctx, result, message)


def resume_copilot_turn(
    graph: Any,
    ctx: CopilotContext,
    approved: bool,
    note: str = "",
) -> dict[str, Any]:
    """Resume a paused confirm_sign_off node with Command(resume=...)."""
    result = graph.invoke(
        Command(resume={"approved": approved, "note": note}),
        config=_run_config(ctx.conversation_id),
        context=ctx,
    )
    return _finalize_graph_result(ctx, result, "")
