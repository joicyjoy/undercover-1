"""Structured data returned and used by the Shoppie Agent."""

from typing import Literal

from pydantic import BaseModel, Field

RefundReason = Literal[
    "cancellation",
    "damaged_or_defective",
    "missing_item",
    "change_of_mind",
    "failed_delivery_customer",
    "failed_delivery_shoppie",
]


class Line(BaseModel):
    """One covered order item and quantity to include in a refund calculation."""

    item_id: str = Field(description="The order item's item_id or sku")
    quantity: int = Field(
        ge=1, description="How many units of this item are covered by the refund"
    )


class Recommendation(BaseModel):
    """Final structured recommendation returned by the Shoppie Agent."""

    outcome: Literal["approve", "decline", "request_information"]
    rationale: str
    policy_citations: list[str]
    next_action: str
    priority_human_review: bool
    refund_amount: float = 0.0
    decided_by: str = "The Shoppie Agent 1"


class TodoItem(BaseModel):
    """One item on the queue agent's to-do list."""

    request_id: str
    title: str
    status: Literal["to_do", "in_progress", "done", "left_for_support"]
