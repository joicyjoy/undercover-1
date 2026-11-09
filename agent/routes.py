"""What the Shoppie Agent's two routes to its tools share: the targets the tools are
on, what a route does, and the Gateway's refusal."""

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Target(StrEnum):
    """Where a tool is: the app, or the context layer."""

    APP = "undercover-app"
    CONTEXT = "undercover-context"


@runtime_checkable
class Route(Protocol):
    """How the Shoppie Agent's tools and its operations reach the app and the context
    layer: through the Gateway, or directly."""

    def call(self, target: Target, operation: str, inputs: dict[str, Any]) -> Any:
        """Call one operation and return its answer, parsed from its JSON."""
        ...

    def name(self, target: Target, operation: str) -> str:
        """Name a call to the operation as this route names it."""
        ...


class GatewayRefused(Exception):
    """The Gateway refused a call, such as a payment or tool its Policy forbids."""

    @property
    def answer(self) -> str:
        """The refusal as a model is answered with it."""
        return f"Refused by the Gateway: {self}"
