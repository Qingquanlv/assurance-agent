"""Deterministic Generation workflow-state handlers."""

from __future__ import annotations

from pydantic import ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.contracts.decisions import (
    GenerationBranchCompletion,
    GenerationCompletionInput,
    GenerationCompletionOutput,
    GenerationReviewRoundAdvanceInput,
    GenerationReviewRoundAdvanceOutput,
    ReviewStage,
    advance_review_round,
    complete_generation,
)
from assurance_generation.operations.planning import InputError, failed_input

REVIEW_ROUND_ADVANCE_ID = "assurance.generation.review-round.advance"
GENERATION_COMPLETE_ID = "assurance.generation.complete"


class GenerationCompleteHandler:
    """Close the four structural lanes into the module's public result."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            output = complete_generation(request.input)
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except (InputError, ValidationError, ValueError) as error:
            return failed_input(InputError(str(error)))


class GenerationReviewRoundAdvanceHandler:
    """Frozen increment of a per-family plan or codegen review counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            output = advance_review_round(request.input)
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except (InputError, ValidationError, ValueError) as error:
            return failed_input(InputError(str(error)))


__all__ = [
    "GENERATION_COMPLETE_ID",
    "GenerationBranchCompletion",
    "GenerationCompleteHandler",
    "GenerationCompletionInput",
    "GenerationCompletionOutput",
    "GenerationReviewRoundAdvanceHandler",
    "GenerationReviewRoundAdvanceInput",
    "GenerationReviewRoundAdvanceOutput",
    "REVIEW_ROUND_ADVANCE_ID",
    "ReviewStage",
]
