"""The Shoppie Agent's tools: backend operations and context tools called on its
route, with errors caught and reported as refusals."""

import contextlib
import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.store.base import BaseStore

from agent import api, memory
from agent.api import Refused
from agent.models import Line, RefundReason
from agent.routes import GatewayRefused, Route, Target

ProgressStage = str


@dataclass
class RequestContext:
    """The request a support agent is working on, and the route its tools take."""

    request_id: str
    route: Route


def format_answer(res: Any) -> str:
    """Format a successful tool answer as readable text or JSON."""
    if isinstance(res, (dict, list)):
        return json.dumps(res, indent=2)
    return str(res)


def call_on_route(
    route: Route,
    request_id: str,
    target: Target,
    operation: str,
    inputs: dict[str, Any],
) -> str:
    """Call an operation on the given route, catching refusals as model answers."""
    try:
        result = route.call(target, operation, inputs)
        return format_answer(result)
    except Refused as error:
        if (
            error.status_code == HTTPStatus.NOT_FOUND
            and operation == "read_policy_section"
        ):
            return "No such section. Use list_policy_sections for the ids."
        name = route.name(target, operation)
        msg = f"{name}: {error.answer}"
        with contextlib.suppress(Exception):
            api.record_step(request_id, "refusal", msg)
        return error.answer
    except GatewayRefused as error:
        name = route.name(target, operation)
        msg = f"{name}: {error.answer}"
        with contextlib.suppress(Exception):
            api.record_step(request_id, "refusal", msg)
        return error.answer
    except ToolException as error:
        name = route.name(target, operation)
        msg = f"{name}: Refused: {error}"
        with contextlib.suppress(Exception):
            api.record_step(request_id, "refusal", msg)
        return f"Refused: {error}"


def _build_memory_tool(
    request_id: str, store: BaseStore, customer_id: str | None
) -> BaseTool:
    @tool
    def search_memory(query: str) -> str:
        """Search what we remember about this customer from earlier requests."""
        cid = customer_id
        if not cid:
            try:
                req_data = api.get_request(request_id)
                cid = req_data.get("customer_id") or req_data.get("customer", {}).get(
                    "customer_id", ""
                )
            except Exception:
                cid = ""
        try:
            found = memory.recall(store, cid, request_id, query)
        except memory.UNREACHABLE as error:
            with contextlib.suppress(Exception):
                api.record_step(
                    request_id,
                    "refusal",
                    f"Refused: The memory can't be reached. {error}",
                )
            raise ToolException(f"The memory can't be reached. {error}") from error
        msg = memory.what_was_found(query, found)
        with contextlib.suppress(Exception):
            api.record_step(request_id, "memory_search", msg)
        return json.dumps(found)

    return search_memory


def make_request_tools(
    request_id: str,
    route: Route,
    store: BaseStore | None = None,
    customer_id: str | None = None,
) -> list[BaseTool]:
    """Create the backend API tools for working one support request on a given route."""

    @tool
    def get_request_context() -> str:
        """Retrieve this request's customer message, evidence, order and history."""
        return call_on_route(
            route,
            request_id,
            Target.APP,
            "get_request_context",
            {"request_id": request_id},
        )

    @tool
    def list_policy_sections() -> str:
        """List Shoppie's policies and the id and title of each section."""
        return call_on_route(route, request_id, Target.APP, "list_policy_sections", {})

    @tool
    def read_policy_section(policy: str, section: str) -> str:
        """Read one policy section by its policy and section id."""
        return call_on_route(
            route,
            request_id,
            Target.APP,
            "read_policy_section",
            {"request_id": request_id, "policy": policy, "section": section},
        )

    @tool
    def calculate_refund(reason: RefundReason, lines: list[Line]) -> str:
        """Work out the eligible INR refund for covered items on this request."""
        line_dicts = [item.model_dump() for item in lines]
        return call_on_route(
            route,
            request_id,
            Target.APP,
            "calculate_refund",
            {
                "request_id": request_id,
                "reason": reason,
                "lines": line_dicts,
            },
        )

    @tool
    def record_progress(stage: str) -> str:
        """Record a supported progress stage for this request."""
        return call_on_route(
            route,
            request_id,
            Target.APP,
            "add_request_step",
            {"request_id": request_id, "stage": stage},
        )

    tools: list[BaseTool] = [
        get_request_context,
        list_policy_sections,
        read_policy_section,
        calculate_refund,
        record_progress,
    ]

    if store is not None:
        tools.append(_build_memory_tool(request_id, store, customer_id))

    return tools
