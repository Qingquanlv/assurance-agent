"""Deterministic Generation workflow-state handlers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.contracts.families import GENERATION_FAMILIES, LayerName
from assurance_generation.operations.planning import InputError, failed_input

REVIEW_ROUND_ADVANCE_ID = "assurance.generation.review-round.advance"
ReviewStage = Literal["plan", "codegen"]
_STAGES: tuple[ReviewStage, ...] = ("plan", "codegen")


class GenerationReviewRoundAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: LayerName
    stage: ReviewStage
    rounds_used: int = Field(ge=0)
    rounds_budget: int = Field(ge=1)

    @property
    def remaining(self) -> int:
        return self.rounds_budget - self.rounds_used


class GenerationReviewRoundAdvanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: LayerName
    stage: ReviewStage
    rounds_used: int = Field(ge=1)
    rounds_budget: int = Field(ge=1)


class GenerationReviewRoundAdvanceHandler:
    """Frozen increment of a per-family plan or codegen review counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = _validate(request.input)
            if payload.family not in GENERATION_FAMILIES:
                raise InputError(f"unknown generation family: {payload.family}")
            if payload.stage not in _STAGES:
                raise InputError(f"unknown generation review stage: {payload.stage}")
            if payload.rounds_used >= payload.rounds_budget:
                raise InputError("rounds_used must be below rounds_budget")
            output = GenerationReviewRoundAdvanceOutput(
                family=payload.family,
                stage=payload.stage,
                rounds_used=payload.rounds_used + 1,
                rounds_budget=payload.rounds_budget,
            )
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)


def _validate(data: object) -> GenerationReviewRoundAdvanceInput:
    try:
        return GenerationReviewRoundAdvanceInput.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
