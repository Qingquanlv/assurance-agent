from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReviewRoundAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds_used: int = Field(ge=0)
    rounds_budget: int = Field(ge=1)

    @property
    def remaining(self) -> int:
        return self.rounds_budget - self.rounds_used


class ReviewRoundAdvanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds_used: int = Field(ge=1)
    rounds_budget: int = Field(ge=1)


def advance_review_round(data: object) -> ReviewRoundAdvanceOutput:
    try:
        payload = ReviewRoundAdvanceInput.model_validate(data)
    except ValidationError as error:
        raise error
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return ReviewRoundAdvanceOutput(
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )


__all__ = [
    "ReviewRoundAdvanceInput",
    "ReviewRoundAdvanceOutput",
    "advance_review_round",
]
