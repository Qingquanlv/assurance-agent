"""Deterministic Intake workflow-state handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.operations.agent_skills import InputError, failed_input

REVIEW_ROUND_ADVANCE_ID = "assurance.intake.review-round.advance"


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


class ReviewRoundAdvanceHandler:
    """Frozen increment of the case-review budget counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = _validate(request.input)
            if payload.rounds_used >= payload.rounds_budget:
                raise InputError("rounds_used must be below rounds_budget")
            output = ReviewRoundAdvanceOutput(
                rounds_used=payload.rounds_used + 1,
                rounds_budget=payload.rounds_budget,
            )
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)


def _validate(data: object) -> ReviewRoundAdvanceInput:
    try:
        return ReviewRoundAdvanceInput.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
