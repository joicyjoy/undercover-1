"""The direct route: the Shoppie Agent's tools and operations straight to the app,
through the agent's API with its key, and the context layer's tools over MCP at
UNDERCOVER_CONTEXT, or on the laptop, unsigned."""

import json
import os
from typing import Any

from agent import api
from agent.mcp_tools import McpTools
from agent.routes import Target

CONTEXT_URL = "UNDERCOVER_CONTEXT"
CONTEXT_LAYER = "The context layer"
LOCAL_CONTEXT = "http://127.0.0.1:8006/mcp"


class Direct:
    """The app's operations and the context layer's tools, called by their own
    names."""

    def __init__(self, context: McpTools | None = None) -> None:
        self.context = context

    def name(self, target: Target, operation: str) -> str:
        return operation

    def call(self, target: Target, operation: str, inputs: dict[str, Any]) -> Any:
        """Call one of the context layer's tools, or the app's operation through the
        agent's API, whose functions are named after the operations."""
        if target == Target.CONTEXT:
            if self.context is None:
                raise ConnectionError("The context layer is not connected")
            return json.loads(self.context.call(operation, inputs))
        op = getattr(api, operation)
        return op(**inputs)


def from_environment() -> Direct:
    """The direct route, with the context layer at UNDERCOVER_CONTEXT, read now, or
    on the laptop, whose tools it lists now."""
    url = os.getenv(CONTEXT_URL)
    if url:
        return Direct(McpTools(CONTEXT_LAYER, url))
    try:
        return Direct(McpTools(CONTEXT_LAYER, LOCAL_CONTEXT))
    except ConnectionError:
        return Direct(None)
