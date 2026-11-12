"""Phase 2 tests for isolated per-handler Copilot memory."""

from langchain_core.messages import AIMessage, HumanMessage

from agent.copilot_memory import (
    make_copilot_memory_store,
    recall_handler_memories,
    save_handler_turn_memory,
)
from agent.tests.memory_stand_in import MemoryStandIn


def test_copilot_memory_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("UNDERCOVER_COPILOT_MEMORY_ID", raising=False)
    assert make_copilot_memory_store() is None
    assert recall_handler_memories(None, "HND-01", "refund limit") == []


def test_copilot_memory_isolates_handlers():
    stand_in = MemoryStandIn(
        memories=[
            ("/handlers/HND-01/preferences/1", "Dana prefers brief INR bullet tables."),
            (
                "/handlers/HND-02/preferences/1",
                "Marcus prefers step-by-step summaries.",
            ),
            ("/handlers/HND-01/facts/1", "Dana covers over-limit Mumbai electronics."),
        ]
    )

    dana_hits = recall_handler_memories(stand_in, "HND-01", "preferences")
    marcus_hits = recall_handler_memories(stand_in, "HND-02", "preferences")

    assert [item["content"] for item in dana_hits] == [
        "Dana prefers brief INR bullet tables.",
        "Dana covers over-limit Mumbai electronics.",
    ]
    assert [item["content"] for item in marcus_hits] == [
        "Marcus prefers step-by-step summaries.",
    ]
    assert all("Marcus" not in item["content"] for item in dana_hits)


def test_save_handler_turn_memory_records_handler_and_conversation_events():
    stand_in = MemoryStandIn()
    written = save_handler_turn_memory(
        stand_in,
        "HND-01",
        "CONV-123",
        [
            HumanMessage(content="Always show INR figures to two decimal places."),
            AIMessage(content="Got it, I will format INR amounts with two decimals."),
        ],
    )
    assert written == 2
    assert len(stand_in.events) == 2
    assert stand_in.events[0].actor == "HND-01"
    assert stand_in.events[0].session == "CONV-123"
