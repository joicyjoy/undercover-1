"""What claims manual section 14 asks of a decision record: the request's checks,
which need nothing from the claim, and the calculation's, each refusing with a 422
whose message names section 14."""

from typing import Any

from fastapi import HTTPException

from backend.models import Calculation, CalculationStep, DecisionKind, DecisionRequest
from backend.money import dollars

SECTION_14 = "Claims manual section 14: {missing}"
NO_REASON = "a decision record needs a reason in plain language"
NO_SECTION = "a decision record needs the section relied on"
NO_MAKER = "a decision record needs who or what made the decision"
NOTHING_APPROVED = "an approval needs a payout above zero"
SOMETHING_PAID = "a decline or a referral has a payout of zero"
NO_CALCULATION = (
    "an approval needs the calculation of its payout for this attempt. Work it out "
    "with calculate_payout"
)
NOT_THE_CALCULATION = "the payout must be the calculation's {payout}"


def falls_short(missing: str) -> HTTPException:
    """Refuse a decision record with what it is missing, naming section 14."""
    return HTTPException(422, SECTION_14.format(missing=missing))


def check_request(request: DecisionRequest) -> None:
    """Refuse a request for a decision that leaves out what section 14 asks for."""
    for text, missing in (
        (request.reason, NO_REASON),
        (request.section, NO_SECTION),
        (request.decided_by, NO_MAKER),
    ):
        if not text.strip():
            raise falls_short(missing)
    approval = request.decision == DecisionKind.APPROVE
    if approval and request.payout <= 0:
        raise falls_short(NOTHING_APPROVED)
    if not approval and request.payout != 0:
        raise falls_short(SOMETHING_PAID)


def check_calculation(
    payout: float, calculation: Calculation | dict[str, Any] | None
) -> Calculation:
    """Return the calculation on file for an approval's attempt, or refuse the
    approval when there is none or its payout isn't the calculation's."""
    if calculation is None:
        raise falls_short(NO_CALCULATION)
    if isinstance(calculation, dict):
        calc_payout = float(calculation.get("payout", calculation.get("refund", 0.0)))
        raw_lines = calculation.get("lines", calculation.get("calculation", []))
        steps = [
            s if isinstance(s, CalculationStep) else CalculationStep(**s)
            for s in raw_lines
        ]
        calc_obj = Calculation(lines=steps, payout=calc_payout)
    else:
        calc_obj = calculation
    if round(payout, 2) != round(calc_obj.payout, 2):
        calculated = dollars(calc_obj.payout)
        raise falls_short(NOT_THE_CALCULATION.format(payout=calculated))
    return calc_obj
