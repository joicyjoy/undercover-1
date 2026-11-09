"""Structured data shared by the backend and the agent."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def to_the_cent(amount: float) -> float:
    """Round an amount to two decimal places."""
    return round(amount, 2)


class DecisionKind(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    REFER = "refer"


class CalculationStep(BaseModel):
    step: str
    amount: float


class Calculation(BaseModel):
    lines: list[CalculationStep]
    payout: float


class Decision(BaseModel):
    decision: DecisionKind
    payout: float
    calculation: Calculation | None = None
    section: str
    reason: str
    decided_by: str
    decided_at: str
    manual_version: str
    signed_off_by: str | None = None
    signed_off_at: str | None = None
    gateway_refusal: str | None = None


def _map_decision_and_payout(mapped: dict[str, Any]) -> None:
    if "outcome" in mapped and "decision" not in mapped:
        outcome = mapped.get("outcome")
        if outcome == "approve":
            mapped["decision"] = "approve"
        elif outcome == "decline":
            mapped["decision"] = "decline"
        elif outcome in ("refer", "request_information"):
            mapped["decision"] = "refer"
    if "refund_amount" in mapped and "payout" not in mapped:
        mapped["payout"] = mapped.get("refund_amount")
    elif "payout" not in mapped and mapped.get("decision") in ("decline", "refer"):
        mapped["payout"] = 0.0


def _map_section_and_maker(mapped: dict[str, Any]) -> None:
    if "rationale" in mapped and "reason" not in mapped:
        mapped["reason"] = mapped.get("rationale")
    if "policy_citations" in mapped and "section" not in mapped:
        citations = mapped.get("policy_citations")
        if isinstance(citations, list):
            mapped["section"] = ", ".join(str(c) for c in citations)
        elif citations:
            mapped["section"] = str(citations)
    if "decided_by" not in mapped:
        if mapped.get("policy_citations") == ["Security check"]:
            mapped["decided_by"] = "Security check"
        else:
            mapped["decided_by"] = "The Shoppie Agent 1"


class DecisionRequest(BaseModel):
    """A decision to record, which claims manual section 14 checks."""

    model_config = ConfigDict(extra="allow")

    decision: DecisionKind
    payout: Annotated[float, AfterValidator(to_the_cent)] = Field(
        description="0 for a decline or referral"
    )
    section: str = Field(
        description="The sections of the wording and the manual relied on"
    )
    reason: str = Field(description="Two or three sentences explaining the decision")
    decided_by: str = Field(
        description=(
            "Who or what made the decision, such as the UnderCover Agent "
            "with the version of its configuration"
        )
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if isinstance(data, dict):
            mapped = dict(data)
            _map_decision_and_payout(mapped)
            _map_section_and_maker(mapped)
            return mapped
        return data


class Recommendation(BaseModel):
    """Final structured recommendation returned by the Shoppie Agent."""

    outcome: Literal["approve", "decline", "request_information"]
    rationale: str
    policy_citations: list[str]
    next_action: str
    priority_human_review: bool
    refund_amount: float = 0.0
    decided_by: str = "The Shoppie Agent 1"
