"""Tests for the Shoppie Agent's two routes: Gateway or direct to the app."""

import asyncio

from langchain_core.messages import AIMessage

from agent import direct, main
from agent.gateway import Gateway
from agent.mcp_tools import McpTools
from agent.routes import Target
from agent.tests.gateway_stand_in import DENIED
from agent.tests.stubs import StubModel, call, screening_stub

APP = "undercover-app___"


def test_the_models_tools_are_the_same_on_both_routes(gateway):
    """Unit test: model tools have identical names and schemas on both routes."""
    direct_route = direct.Direct()
    gateway_route = Gateway(gateway.url)

    direct_tools = main.make_request_tools("REQ-7001", direct_route)
    gateway_tools = main.make_request_tools("REQ-7001", gateway_route)

    direct_schemas = [(t.name, t.args) for t in direct_tools]
    gateway_schemas = [(t.name, t.args) for t in gateway_tools]

    assert direct_schemas == gateway_schemas
    for name, args in direct_schemas:
        assert "request_id" not in args, (
            f"Tool {name} must not expose request_id to model"
        )


def test_the_gateway_route_executes_through_the_gateway(api, gateway):
    """Stub test: request execution calls Gateway operations signed with SigV4."""
    gateway_route = Gateway(gateway.url)
    lines = [{"item_id": "ITM-10001-1", "quantity": 1}]
    model = StubModel(
        messages=iter(
            [
                call("get_request_context"),
                call(
                    "calculate_refund",
                    {"reason": "cancellation", "lines": lines},
                ),
                AIMessage(content="I have calculated the refund."),
                call(
                    "Recommendation",
                    {
                        "outcome": "approve",
                        "rationale": "Eligible cancellation.",
                        "policy_citations": ["cancellation"],
                        "next_action": "Support agent review.",
                        "priority_human_review": False,
                    },
                ),
            ]
        )
    )

    result = main.work_request("REQ-7001", llm=model, route=gateway_route)
    assert result["status"] == "done"

    # Now simulate human review and resume to complete request
    api.post(
        "/api/requests/REQ-7001/review",
        json={
            "outcome": "approve",
            "notes": "Approved",
            "reviewed_by": "Support Priya",
        },
    )
    resumed = main.work_request("REQ-7001", llm=model, route=gateway_route)
    assert resumed["status"] == "done"

    assert gateway.names == [
        f"{APP}get_request_context",
        f"{APP}calculate_refund",
        f"{APP}save_recommendation",
        f"{APP}complete_request",
    ]
    assert all(c.signed for c in gateway.calls)


def test_bookkeeping_and_security_screening_skip_the_gateway(api, gateway, monkeypatch):
    """Stub test: screening and bookkeeping reach backend directly with X-API-Key."""
    gateway_route = Gateway(gateway.url)
    flagging_screener = screening_stub(
        instructions_found=True,
        reason="Found prompt injection in customer message.",
    )
    monkeypatch.setattr(main, "screening_model", lambda: flagging_screener)

    # REQ-7901 is the security test request with injection
    result = main.work_request(
        "REQ-7901",
        llm=StubModel(messages=iter([])),
        route=gateway_route,
    )
    assert result["status"] == "done"
    # The security check held the request without touching Gateway tools
    assert gateway.calls == []


def test_get_request_context_gives_the_same_text_on_both_routes(api, gateway):
    """Stub test: get_request_context produces identical output on both routes."""
    api.post("/api/requests/REQ-7001/start")
    direct_route = direct.Direct()
    gateway_route = Gateway(gateway.url)

    direct_output = direct_route.call(
        Target.APP, "get_request_context", {"request_id": "REQ-7001"}
    )
    gateway_output = gateway_route.call(
        Target.APP, "get_request_context", {"request_id": "REQ-7001"}
    )

    assert direct_output == gateway_output


def test_an_app_refusal_is_a_clean_refusal_step_and_work_continues(api, gateway):
    """Stub test: app refusal returns clean message without halting work."""
    gateway_route = Gateway(gateway.url)
    huge_lines = [{"item_id": "ITM-10002-1", "quantity": 500}]
    valid_lines = [{"item_id": "ITM-10002-1", "quantity": 1}]

    model = StubModel(
        messages=iter(
            [
                call("get_request_context"),
                call(
                    "calculate_refund",
                    {"reason": "cancellation", "lines": huge_lines},
                ),
                call(
                    "calculate_refund",
                    {"reason": "cancellation", "lines": valid_lines},
                ),
                AIMessage(content="Recalculated refund successfully."),
                call(
                    "Recommendation",
                    {
                        "outcome": "approve",
                        "rationale": "Eligible cancellation after recalculation.",
                        "policy_citations": ["cancellation"],
                        "next_action": "Support agent review.",
                        "priority_human_review": False,
                    },
                ),
            ]
        )
    )

    result = main.work_request("REQ-7002", llm=model, route=gateway_route)
    assert result["status"] == "done"

    req = api.get("/api/requests/REQ-7002").json()
    refusals = [s for s in req["steps"] if s["stage"] == "refusal"]
    assert len(refusals) >= 1
    assert "approval limit" in refusals[0]["message"]


def test_a_tool_the_gateway_refuses_is_a_refusal_and_the_work_goes_on(api, gateway):
    """Stub test: tool refused by Gateway returns refusal and records step."""
    gateway_route = Gateway(gateway.url)
    gateway.refused.add(f"{APP}calculate_refund")
    lines = [{"item_id": "ITM-10003-1", "quantity": 1}]

    model = StubModel(
        messages=iter(
            [
                call("get_request_context"),
                call(
                    "calculate_refund",
                    {"reason": "cancellation", "lines": lines},
                ),
                AIMessage(content="Refund calculation refused by policy."),
                call(
                    "Recommendation",
                    {
                        "outcome": "decline",
                        "rationale": "Cannot calculate refund due to policy denial.",
                        "policy_citations": ["cancellation"],
                        "next_action": "Support agent review.",
                        "priority_human_review": True,
                    },
                ),
            ]
        )
    )

    result = main.work_request("REQ-7003", llm=model, route=gateway_route)
    assert result["status"] == "done"

    req = api.get("/api/requests/REQ-7003").json()
    refusals = [s for s in req["steps"] if s["stage"] == "refusal"]
    assert len(refusals) >= 1
    assert "Refused by the Gateway" in refusals[0]["message"]
    assert DENIED in refusals[0]["message"]


def test_a_payment_the_gateway_refuses_waits_for_sign_off(api, gateway):
    """Stub test: payment refused by Gateway holds request with refusal recorded."""
    gateway_route = Gateway(gateway.url)
    gateway.refused.add(f"{APP}complete_request")
    lines = [{"item_id": "ITM-10004-1", "quantity": 1}]

    model = StubModel(
        messages=iter(
            [
                call("get_request_context"),
                call(
                    "calculate_refund",
                    {
                        "reason": "cancellation",
                        "lines": lines,
                    },
                ),
                AIMessage(content="Calculated refund."),
                call(
                    "Recommendation",
                    {
                        "outcome": "approve",
                        "rationale": "Eligible cancellation.",
                        "policy_citations": ["cancellation"],
                        "next_action": "Support agent review.",
                        "priority_human_review": False,
                    },
                ),
            ]
        )
    )

    # First run reaches recommendation
    res1 = main.work_request("REQ-7004", llm=model, route=gateway_route)
    assert res1["status"] == "done"

    # Human signs off
    api.post(
        "/api/requests/REQ-7004/review",
        json={
            "outcome": "approve",
            "notes": "Approved by human",
            "reviewed_by": "Support Priya",
        },
    )

    # Resuming attempts complete_request, which Gateway refuses
    res2 = main.work_request("REQ-7004", llm=model, route=gateway_route)
    assert res2["status"] == "done"

    req = api.get("/api/requests/REQ-7004").json()
    # It must be waiting for review with gateway refusal recorded and paused!
    assert req["status"] == "waiting_for_agent_review"
    assert req["recommendation"]["gateway_refusal"] == DENIED
    assert main.is_paused("REQ-7004", attempt=1)
    refusals = [s for s in req["steps"] if s["stage"] == "refusal"]
    assert len(refusals) >= 1
    assert "refused by the gateway" in refusals[-1]["message"].lower()


def test_an_mcp_call_leaves_the_main_threads_event_loop(api, gateway):
    """Unit test: verifies get_event_loop() remains functional on main thread."""
    loop = asyncio.get_event_loop()
    tools = McpTools("The stand-in Gateway", gateway.url)
    answer = tools.call(f"{APP}get_request_context", {"request_id": "REQ-7001"})
    assert answer is not None
    assert asyncio.get_event_loop() is loop
    assert not loop.is_closed()


def test_dynamic_env_read_when_building_graph(gateway, monkeypatch):
    """Test reading UNDERCOVER_GATEWAY_URL when building graph."""
    monkeypatch.setenv("UNDERCOVER_GATEWAY_URL", gateway.url)
    route = main.get_route()
    assert isinstance(route, Gateway)
    monkeypatch.delenv("UNDERCOVER_GATEWAY_URL")
    route_direct = main.get_route()
    assert isinstance(route_direct, direct.Direct)
