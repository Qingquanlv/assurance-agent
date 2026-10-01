"""Case-review round budget arithmetic."""

from __future__ import annotations

from assurance_intake.contracts.decisions import ReviewRoundAdvanceInput, ReviewRoundAdvanceOutput


def advance_review_round(data: object) -> ReviewRoundAdvanceOutput:
    payload = ReviewRoundAdvanceInput.model_validate(data)
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return ReviewRoundAdvanceOutput(
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )


__all__ = ["advance_review_round"]
