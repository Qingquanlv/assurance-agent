"""Deterministic Intake workflow-state handlers.

The typed graph calls ``advance_review_round`` as an ordinary node. This
handler remains the YAML adapter and discards ``TaskContext``.
"""

from __future__ import annotations

from pydantic import ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.decisions import (
    ReviewRoundAdvanceInput,
    ReviewRoundAdvanceOutput,
    advance_review_round,
)
from assurance_intake.operations.prepare import InputError, failed_input

REVIEW_ROUND_ADVANCE_ID = "assurance.intake.review-round.advance"


class ReviewRoundAdvanceHandler:
    """Frozen increment of the case-review budget counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            output = advance_review_round(request.input)
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except (InputError, ValidationError, ValueError) as error:
            return failed_input(InputError(str(error)))


__all__ = [
    "REVIEW_ROUND_ADVANCE_ID",
    "ReviewRoundAdvanceHandler",
    "ReviewRoundAdvanceInput",
    "ReviewRoundAdvanceOutput",
]
