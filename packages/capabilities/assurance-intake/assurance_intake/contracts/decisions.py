from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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


__all__ = [
    "ReviewRoundAdvanceInput",
    "ReviewRoundAdvanceOutput",
]
