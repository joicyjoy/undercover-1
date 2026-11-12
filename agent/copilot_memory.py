"""Per-handler memory adapter backed by Bedrock AgentCore Memory."""

from __future__ import annotations

import os

from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.store.base import BaseStore
from langgraph_checkpoint_aws import AgentCoreMemoryStore

COPILOT_MEMORY_ENV = "UNDERCOVER_COPILOT_MEMORY_ID"
DEFAULT_COPILOT_MEMORY_NAME = "undercover_copilot_memory"
MAX_MEMORY_RESULTS = 6
UNREACHABLE = (BotoCoreError, ClientError)


def copilot_memory_id() -> str | None:
    """Return the configured Copilot Memory ID, or None when disabled."""
    value = os.getenv(COPILOT_MEMORY_ENV, "").strip()
    return value or None


def make_copilot_memory_store(region_name: str | None = None) -> BaseStore | None:
    """Open the Copilot AgentCoreMemoryStore when configured."""
    mem_id = copilot_memory_id()
    if not mem_id:
        return None
    region = (
        region_name
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return AgentCoreMemoryStore(memory_id=mem_id, region_name=region)


def recall_handler_memories(
    store: BaseStore | None, handler_id: str, query: str
) -> list[dict[str, str]]:
    """Search one handler's preferences and facts without crossing handlers."""
    if store is None or not handler_id.strip() or not query.strip():
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for bucket, prefix in (
        ("preference", ("handlers", handler_id, "preferences")),
        ("fact", ("handlers", handler_id, "facts")),
    ):
        items = store.search(prefix, query=query.strip(), limit=MAX_MEMORY_RESULTS)
        for item in items:
            val = item.value if isinstance(item.value, dict) else {}
            namespaces = val.get("namespaces") or []
            expected_prefix = f"/handlers/{handler_id}/"
            if namespaces and not any(
                str(ns).startswith(expected_prefix) for ns in namespaces
            ):
                continue
            text = str(val.get("content") or val.get("text") or "").strip()
            if text and text not in seen:
                seen.add(text)
                results.append({"kind": bucket, "content": text})
    return results


def save_handler_turn_memory(
    store: BaseStore | None,
    handler_id: str,
    conversation_id: str,
    messages: list[BaseMessage],
) -> int:
    """Write recent non-sensitive turn messages to AgentCore Memory for one handler."""
    if store is None or not handler_id.strip() or not conversation_id.strip():
        return 0
    written = 0
    namespace = (handler_id, conversation_id)
    for index, msg in enumerate(messages):
        if not isinstance(msg, (HumanMessage, AIMessage)):
            continue
        text = str(msg.content).strip()
        if not text:
            continue
        role = "human" if isinstance(msg, HumanMessage) else "ai"
        key = f"turn-{index}-{role}"
        store.put(namespace, key, {"message": msg})
        written += 1
    return written
