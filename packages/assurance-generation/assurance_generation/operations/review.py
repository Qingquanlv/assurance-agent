"""Constructor-closed four-family plan-review prepare/finalize handlers."""

from __future__ import annotations

from pydantic import ValidationError

from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import AgentBindingDataV1, AgentFinalizeInputV1
from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.operations.planning import (
    FAMILIES,
    PLAN_REVIEW_RESULT_ID,
    REVIEW_PERSONA,
    Family,
    InputError,
    OutputError,
    closed_family,
    failed_input,
    failed_output,
    leafs_of,
    prepare_plan_outcome,
    validate_plan_input,
)

_REVIEW_SKILL_FILES: dict[Family, str] = {
    "api": "skills/aa-api-plan-reviewer/SKILL.md",
    "e2e": "skills/aa-e2e-plan-reviewer/SKILL.md",
    "fuzz": "skills/aa-fuzz-plan-reviewer/SKILL.md",
    "performance": "skills/aa-performance-plan-reviewer/SKILL.md",
}


class PlanReviewPrepareHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business, cases = validate_plan_input(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            return prepare_plan_outcome(
                family=self._family,
                skill_path=_REVIEW_SKILL_FILES[self._family],
                persona_path=REVIEW_PERSONA,
                business=business,
                cases=cases,
                binding=binding,
                result_schema_id=PLAN_REVIEW_RESULT_ID,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class PlanReviewFinalizeHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = AgentFinalizeInputV1.model_validate(request.input)
            try:
                document = PlanReviewAuthoring.model_validate(
                    thaw_json(payload.agent_result.structured_result),
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = f"{self._family}-plan"
            if document.review_type != expected:
                raise OutputError(f"review_type {document.review_type!r} does not match {expected}")
            return TaskOutcome.succeeded(document.model_dump(mode="json"))
        except ValidationError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def review_prepare_handler(family: str) -> TaskHandler:
    return PlanReviewPrepareHandler(closed_family(family))


def review_finalize_handler(family: str) -> TaskHandler:
    return PlanReviewFinalizeHandler(closed_family(family))


__all__ = [
    "FAMILIES",
    "PlanReviewFinalizeHandler",
    "PlanReviewPrepareHandler",
    "review_finalize_handler",
    "review_prepare_handler",
]
