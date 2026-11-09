"""Tests for the agent's memory of each customer: what it saves of a decided request,
what a search of customer memories finds, error handling when unreachable, and
omission when memory is off or in evaluation runs."""

import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent import api, main, memory
from agent.memory import MEMORY_ID
from agent.tests.memory_stand_in import MemoryStandIn
from agent.tests.stubs import (
    CITATION,
    FlakyModel,
    StubModel,
    call,
    screening_stub,
    scripted_model,
)

FACTS = "/customers/CUS-1008/facts/preferences"
FIRST_REQ = "/customers/CUS-1008/claims/REQ-7007/"
SECOND_REQ = "/customers/CUS-1008/claims/REQ-7008/"
SOMEONE_ELSES = "/customers/CUS-1001/claims/REQ-7001/"


def tools_of(graph: Any) -> set[str]:
    """Return the names of the tools bound to the tools node."""
    tools_node = graph.nodes["tools"].bound
    if hasattr(tools_node, "tools_by_name"):
        return set(tools_node.tools_by_name.keys())
    return {getattr(t, "name", str(t)) for t in getattr(tools_node, "tools", [])}


def test_an_approval_saves_the_request_and_the_decision(store):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn()
    result = main.run_request(
        "REQ-7008",
        "thread-mem-1",
        scripted_model(outcome="approve"),
        store=mem_store,
    )
    assert result["outcome"] == "approve"
    assert len(mem_store.events) == 2
    requested, decided = mem_store.events

    assert (requested.actor, requested.session) == ("CUS-1008", "REQ-7008")
    assert (decided.actor, decided.session) == ("CUS-1008", "REQ-7008")
    assert isinstance(requested.message, HumanMessage)
    assert isinstance(decided.message, AIMessage)

    req_record = store.get_request("REQ-7008")
    req_json = req_record["request"]
    assert req_json["request_id"] in requested.message.content
    assert req_json["type"] in requested.message.content
    assert "REQ-7008" in decided.message.content
    assert "approve" in decided.message.content

    # Verify story / customer message is kept off memory
    assert req_json["customer_message"] not in requested.message.content
    assert req_json["customer_message"] not in decided.message.content


@pytest.mark.parametrize("outcome", ["approve", "decline", "request_information"])
def test_each_decision_outcome_is_saved(store, outcome):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn()
    result = main.run_request(
        "REQ-7008",
        f"thread-{outcome}",
        scripted_model(outcome=outcome),
        store=mem_store,
    )
    assert result["outcome"] == outcome
    assert len(mem_store.events) == 2
    assert [(e.actor, e.session) for e in mem_store.events] == [
        ("CUS-1008", "REQ-7008"),
        ("CUS-1008", "REQ-7008"),
    ]


def test_a_request_flagged_by_security_check_is_never_saved(store, monkeypatch):
    assert store.start_request("REQ-7008")
    monkeypatch.setattr(
        main,
        "screening_model",
        lambda: screening_stub(
            instructions_found=True, reason="Customer message instructs agent."
        ),
    )
    mem_store = MemoryStandIn()
    result = main.run_request(
        "REQ-7008", "thread-sec", scripted_model(), store=mem_store
    )
    assert result["policy_citations"] == ["Security check"]
    assert mem_store.events == []


def test_evaluation_copy_does_not_save_to_memory(store, monkeypatch):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn()
    orig_get = api.get_request
    monkeypatch.setattr(
        api,
        "get_request",
        lambda req_id: {
            **orig_get(req_id),
            "copy_of": "REQ-7001",
            "evaluation_id": "eval-1",
        },
    )
    result = main.run_request(
        "REQ-7008",
        "thread-eval",
        scripted_model(),
        store=mem_store,
    )
    assert result["outcome"] == "approve"
    assert mem_store.events == []


def test_request_failing_before_decision_is_never_saved(store):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn()
    failing_model = FlakyModel(messages=iter([call("get_request_context")]))
    failing_model.failures = 10
    with pytest.raises(ConnectionError, match="provider timeout"):
        main.run_request("REQ-7008", "thread-fail", failing_model, store=mem_store)
    assert mem_store.events == []


def test_search_memory_recalls_customer_memories_and_excludes_own_request(store):
    mem_store = MemoryStandIn(
        memories=[
            (FACTS, "Customer prefers morning delivery."),
            (FIRST_REQ, "REQ-7007 was approved for refund."),
            (SECOND_REQ, "REQ-7008 summary from earlier attempt."),
            (SOMEONE_ELSES, "Aarav Mehta's cancellation request was approved."),
        ]
    )
    found = memory.recall(mem_store, "CUS-1008", "REQ-7008", "preferences and history")
    assert mem_store.searched == ["/customers/CUS-1008"]
    assert found == [
        "Customer prefers morning delivery.",
        "REQ-7007 was approved for refund.",
    ]


def test_memory_unreachable_logs_warning_on_save_and_request_succeeds(store, caplog):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn(reachable=False)
    with caplog.at_level(logging.WARNING):
        result = main.run_request(
            "REQ-7008",
            "thread-unreachable",
            scripted_model(outcome="decline"),
            store=mem_store,
        )
    assert result["outcome"] == "decline"
    record = store.get_request("REQ-7008")
    assert record["recommendation"]["outcome"] == "decline"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("EndpointConnectionError" in r.getMessage() for r in warnings)


def test_memory_unreachable_refuses_search_and_records_refusal_step(store):
    assert store.start_request("REQ-7008")
    mem_store = MemoryStandIn(reachable=False)
    # Model searches memory, which refuses, then proceeds to recommendation
    replies = [
        call("get_request_context"),
        call("search_memory", {"query": "earlier requests"}),
        AIMessage(content="I have reviewed the memory search result."),
        call(
            "Recommendation",
            {
                "outcome": "decline",
                "rationale": "Policy not met.",
                "policy_citations": [CITATION],
                "next_action": "Support review.",
                "priority_human_review": False,
            },
        ),
    ]
    search_model = StubModel(messages=iter(replies))
    result = main.run_request(
        "REQ-7008", "thread-search-unreachable", search_model, store=mem_store
    )
    assert result["outcome"] == "decline"
    steps = store.get_request("REQ-7008")["steps"]
    refusal_steps = [s for s in steps if s["stage"] == "refusal"]
    assert len(refusal_steps) >= 1
    assert any("The memory can't be reached" in s["message"] for s in refusal_steps)


def test_without_memory_store_search_memory_tool_and_remember_node_are_omitted(
    store,
):
    graph_without = main.build_request_graph("REQ-7008", store=None)
    assert main.REMEMBER not in graph_without.nodes
    assert "search_memory" not in tools_of(graph_without)

    mem_store = MemoryStandIn()
    graph_with = main.build_request_graph("REQ-7008", store=mem_store)
    assert main.REMEMBER in graph_with.nodes
    assert "search_memory" in tools_of(graph_with)


def test_agentcore_memory_store_instantiation(monkeypatch):
    instantiated = []

    def fake_store(**kwargs):
        instantiated.append(kwargs)
        return MemoryStandIn()

    monkeypatch.setattr(memory, "AgentCoreMemoryStore", fake_store)
    monkeypatch.setenv(MEMORY_ID, "undercover-mem-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    store = memory.memory_store()
    assert store is not None
    assert instantiated == [
        {"memory_id": "undercover-mem-test", "region_name": "us-east-1"}
    ]

    monkeypatch.delenv(MEMORY_ID, raising=False)
    assert memory.memory_store() is None
