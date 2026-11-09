"""A stand-in for the AgentCore Gateway: an MCP server whose tools are named as the
Gateway names them. undercover-app___<operation> is each operation in the agent's
API, sent to the test backend with the agent's key, and refusals come back as
tool errors with the app's answer. Its Policy refuses payment over the limit and
any tool a test names with an MCP error, as the Gateway's Policy does. It keeps each
call's name, its inputs and whether it was signed with SigV4 for bedrock-agentcore."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
from fastapi.testclient import TestClient
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from starlette.applications import Starlette
from starlette.routing import Route

from agent.gateway import SEPARATOR, SERVICE
from agent.routes import Target
from backend.main import app

APP = f"{Target.APP}{SEPARATOR}"
DENIED = "Tool Execution Denied: Tool call not allowed due to policy enforcement"
SIGNED_FOR = f"/{SERVICE}/aws4_request"


@dataclass
class Operation:
    """One of the app's operations, as the Gateway's tool for it calls it."""

    method: str
    path: str
    in_path: list[str]
    in_query: list[str]
    tool: types.Tool


@dataclass
class Call:
    """A call to one of the stand-in's tools."""

    name: str
    inputs: dict[str, Any]
    signed: bool


@dataclass
class OpSpec:
    """Inputs and schema for an operation in the stand-in."""

    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    in_path: list[str] = field(default_factory=list)
    in_query: list[str] | None = None


def default_operations() -> dict[str, Operation]:
    """Define the Gateway operations for the Shoppie backend."""
    ops: dict[str, Operation] = {}

    def add_op(
        op_id: str,
        method: str,
        path: str,
        desc: str,
        spec: OpSpec,
    ) -> None:
        name = f"{APP}{op_id}"
        ops[name] = Operation(
            method=method,
            path=path,
            in_path=spec.in_path,
            in_query=spec.in_query or [],
            tool=types.Tool(
                name=name,
                description=desc,
                inputSchema={
                    "type": "object",
                    "properties": spec.properties,
                    "required": spec.required,
                },
            ),
        )

    add_op(
        "get_request_context",
        "GET",
        "/api/requests/{request_id}/context",
        "Retrieve request context",
        OpSpec(
            properties={"request_id": {"type": "string"}},
            required=["request_id"],
            in_path=["request_id"],
        ),
    )
    add_op(
        "list_policy_sections",
        "GET",
        "/api/policies",
        "List policies",
        OpSpec(),
    )
    add_op(
        "read_policy_section",
        "GET",
        "/api/requests/{request_id}/policies/{policy}/sections/{section}",
        "Read policy section",
        OpSpec(
            properties={
                "request_id": {"type": "string"},
                "policy": {"type": "string"},
                "section": {"type": "string"},
            },
            required=["request_id", "policy", "section"],
            in_path=["request_id", "policy", "section"],
        ),
    )
    add_op(
        "calculate_refund",
        "POST",
        "/api/requests/{request_id}/refund",
        "Calculate refund",
        OpSpec(
            properties={
                "request_id": {"type": "string"},
                "reason": {"type": "string"},
                "lines": {"type": "array"},
            },
            required=["request_id", "reason", "lines"],
            in_path=["request_id"],
        ),
    )
    add_op(
        "save_recommendation",
        "POST",
        "/api/requests/{request_id}/recommendation",
        "Save recommendation",
        OpSpec(
            properties={
                "request_id": {"type": "string"},
                "outcome": {"type": "string"},
                "rationale": {"type": "string"},
                "policy_citations": {"type": "array"},
                "next_action": {"type": "string"},
                "priority_human_review": {"type": "boolean"},
            },
            required=[
                "request_id",
                "outcome",
                "rationale",
                "policy_citations",
                "next_action",
            ],
            in_path=["request_id"],
        ),
    )
    add_op(
        "complete_request",
        "POST",
        "/api/requests/{request_id}/complete",
        "Complete request",
        OpSpec(
            properties={
                "request_id": {"type": "string"},
                "outcome": {"type": "string"},
                "reviewed_by": {"type": "string"},
                "notes": {"type": "string"},
                "reviewed_at": {"type": "string"},
            },
            required=["request_id", "outcome", "reviewed_by"],
            in_path=["request_id"],
        ),
    )
    add_op(
        "add_request_step",
        "POST",
        "/api/requests/{request_id}/steps",
        "Record progress step",
        OpSpec(
            properties={
                "request_id": {"type": "string"},
                "stage": {"type": "string"},
                "message": {"type": "string"},
            },
            required=["request_id", "stage"],
            in_path=["request_id"],
        ),
    )
    add_op(
        "start_request",
        "POST",
        "/api/requests/{request_id}/start",
        "Start request",
        OpSpec(
            properties={"request_id": {"type": "string"}},
            required=["request_id"],
            in_path=["request_id"],
        ),
    )
    return ops


@dataclass
class GatewayStandIn:
    """The stand-in Gateway, the calls made to it and the tools a test has its
    Policy refuse."""

    url: str = ""
    calls: list[Call] = field(default_factory=list)
    refused: set[str] = field(default_factory=set)
    operations: dict[str, Operation] = field(default_factory=default_operations)
    backend: TestClient = field(default_factory=lambda: TestClient(app))
    server: Server = field(default_factory=lambda: Server("shoppie-gateway"))

    def __post_init__(self) -> None:
        self.server.request_handlers[types.ListToolsRequest] = self.list_tools
        self.server.request_handlers[types.CallToolRequest] = self.call_tool

    def reset(self) -> None:
        """Forget the calls made, and clear refused tools."""
        self.calls.clear()
        self.refused.clear()

    @property
    def names(self) -> list[str]:
        """The names of the tools called, in order."""
        return [call.name for call in self.calls]

    async def list_tools(self, _: types.ListToolsRequest) -> types.ServerResult:
        tools = [operation.tool for operation in self.operations.values()]
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def call_tool(self, request: types.CallToolRequest) -> types.ServerResult:
        name, inputs = request.params.name, request.params.arguments or {}
        http = self.server.request_context.request
        signed = http is not None and SIGNED_FOR in http.headers.get(
            "authorization", ""
        )
        self.calls.append(Call(name, inputs, signed))
        if self.policy_refuses(name, inputs):
            raise McpError(types.ErrorData(code=types.INVALID_REQUEST, message=DENIED))
        answer, refused = await anyio.to_thread.run_sync(self.send, name, inputs)
        text = types.TextContent(type="text", text=answer)
        return types.ServerResult(types.CallToolResult(content=[text], isError=refused))

    def policy_refuses(self, name: str, inputs: dict[str, Any]) -> bool:
        return name in self.refused

    def send(self, name: str, inputs: dict[str, Any]) -> tuple[str, bool]:
        operation = self.operations[name]
        path = operation.path.format(**{key: inputs[key] for key in operation.in_path})
        params = {key: inputs[key] for key in operation.in_query}
        named = {*operation.in_path, *operation.in_query}
        body = {key: value for key, value in inputs.items() if key not in named}
        key = os.getenv("UNDERCOVER_API_KEY", "test-agent-secret-key-0123456789abcdef")
        response = self.backend.request(
            operation.method,
            path,
            params=params,
            json=body or None,
            headers={"X-API-Key": key},
        )
        return response.text, response.is_error

    def asgi(self) -> Starlette:
        manager = StreamableHTTPSessionManager(
            self.server, stateless=True, json_response=True
        )

        @asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            async with manager.run():
                yield

        mcp = Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))
        return Starlette(routes=[mcp], lifespan=lifespan)
