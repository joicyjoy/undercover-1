"""Structured data shared by the Shoppie backend and the Shoppie Agent."""

from typing import Literal

from pydantic import BaseModel


class Recommendation(BaseModel):
    """Final structured recommendation returned by the Shoppie Agent."""

    outcome: Literal["approve", "decline", "request_information"]
    rationale: str
    policy_citations: list[str]
    next_action: str
    priority_human_review: bool
