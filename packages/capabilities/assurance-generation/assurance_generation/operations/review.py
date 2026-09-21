"""Capability-closed four-family plan-review prepare/finalize handlers."""

from __future__ import annotations

import json
from typing import cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import AgentBindingDataV1, AgentFinalizeInputV1
from assurance_generation.contracts.reviews import PlanReviewAuthoring, public_review_outcome
from assurance_generation.operations.planning import (
    FAMILIES,
    PLAN_REVIEW_RESULT_ID,
    REVIEW_PERSONA,
    Family,
    InputError,
    OutputError,
    closed_family,
    expected_plan_review_history,
    failed_input,
    failed_output,
    evidence_ref,
    leafs_of,
    plan_review_outputs,
    plan_review_input_paths,
    prepare_plan_outcome,
    plan_repair_review,
    resolve_family,
)
from assurance_generation.operations.plan_review_policy import (
    apply_plan_review_policy,
    write_finding_scope,
)

_REVIEW_SKILL_FILES: dict[Family, str] = {
    "api": "skills/aa-api-codegen-reviewer/SKILL.md",
    "e2e": "skills/aa-e2e-codegen-reviewer/SKILL.md",
    "fuzz": "skills/aa-fuzz-codegen-reviewer/SKILL.md",
    "performance": "skills/aa-performance-codegen-reviewer/SKILL.md",
}


class PlanReviewPrepareHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            from assurance_generation.contracts.agent import PlanInputV1
            from assurance_generation.operations.codegen import validate_codegen_input

            codegen_business, scope, cases = validate_codegen_input(
                request.input,
                family,
                context.project_root,
            )
            if codegen_business.codegen_output is None:
                raise InputError("codegen_output is required for codegen review")
            business = PlanInputV1.model_validate(
                {
                    "change_id": codegen_business.change_id,
                    "plan_digest": codegen_business.plan_digest,
                    "plan_ref": codegen_business.plan_ref.model_dump(mode="json"),
                    "capability_leafs": list(codegen_business.capability_leafs),
                    "artifact_paths": list(codegen_business.artifact_paths),
                    "reviewed_cases": cases.model_dump(mode="json"),
                    "family_constraints": codegen_business.family_constraints.model_dump(mode="json")
                    if codegen_business.family_constraints is not None
                    else None,
                    "coverage_epoch": codegen_business.coverage_epoch,
                    "local_round": codegen_business.local_round,
                    "reviewed_case": None
                    if codegen_business.reviewed_case is None
                    else codegen_business.reviewed_case.model_dump(mode="json"),
                }
            )
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            review_inputs = plan_review_input_paths(
                context.project_root,
                change_id=business.change_id,
                family=family,
            )
            return prepare_plan_outcome(
                family=family,
                skill_path=_REVIEW_SKILL_FILES[family],
                persona_path=REVIEW_PERSONA,
                business=business,
                cases=cases,
                binding=binding,
                result_schema_id=PLAN_REVIEW_RESULT_ID,
                context=context,
                allowed_outputs=plan_review_outputs(
                    business.change_id,
                    family,
                    coverage_epoch=business.coverage_epoch,
                    review_round=business.local_round,
                ),
                close_result_capabilities=True,
                review_input_paths=review_inputs,
                extra_json={
                    "codegen_scope": scope.model_dump(mode="json"),
                    "codegen_output": codegen_business.codegen_output,
                },
                repair_review=plan_repair_review(
                    context.project_root,
                    business=business,
                    family=family,
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class PlanReviewFinalizeHandler:
    input_model = AgentFinalizeInputV1

    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            payload = AgentFinalizeInputV1.model_validate(request.input)
            raw = thaw_json(payload.agent_result.result_payload)
            if isinstance(raw, dict):
                raw.pop("review_audit", None)
            try:
                document = PlanReviewAuthoring.model_validate(
                    raw,
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = f"{family}-codegen"
            if document.review_type != expected:
                raise OutputError(f"review_type {document.review_type!r} does not match {expected}")
            document = PlanReviewAuthoring.model_validate(
                apply_plan_review_policy(document.model_dump(mode="json"), previous=None),
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            extra: dict[str, object] = {
                "public_outcome": public_review_outcome(document.route),
            }
            if payload.change_id is not None:
                if payload.change_id != document.change_id:
                    raise OutputError("plan review change_id does not match locked change_id")
                input_paths = plan_review_input_paths(
                    context.project_root,
                    change_id=document.change_id,
                    family=family,
                )
                input_refs = tuple(evidence_ref(context.project_root, path) for path in input_paths)
                review_ref = evidence_ref(
                    context.write_root,
                    f"qa/results/review/{family}-codegen-review.json",
                )
                try:
                    authored = json.loads((context.write_root / review_ref.path).read_text(encoding="utf-8"))
                    if isinstance(authored, dict):
                        authored.pop("review_audit", None)
                    authored_document = PlanReviewAuthoring.model_validate(
                        authored,
                        context={"capability_leafs": leafs_of(payload.capability_leafs)},
                    )
                    authored_document = PlanReviewAuthoring.model_validate(
                        apply_plan_review_policy(authored_document.model_dump(mode="json"), previous=None),
                        context={"capability_leafs": leafs_of(payload.capability_leafs)},
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    raise OutputError(f"invalid review artifact: {error}") from error
                if authored_document != document:
                    raise OutputError("review artifact does not match the returned agent result")
                history_relative = (
                    f"qa/results/codegen/{family}/reviews/epochs/"
                    f"{payload.coverage_epoch}/rounds/{payload.local_round}.json"
                )
                expected_history = expected_plan_review_history(
                    change_id=document.change_id,
                    coverage_epoch=payload.coverage_epoch,
                    family=family,
                    round_index=payload.local_round,
                    outcome=str(extra["public_outcome"]),
                    input_refs=input_refs,
                    source_refs=(*input_refs, review_ref),
                )
                history_path = context.write_root / history_relative
                history_path.parent.mkdir(parents=True, exist_ok=True)
                history_path.write_bytes(
                    canonical_json_bytes(expected_history.model_dump(mode="json")) + b"\n"
                )
                history_ref = evidence_ref(context.write_root, history_relative)
                extra["history_ref"] = history_ref.model_dump(mode="json")
                try:
                    scope_relative = write_finding_scope(
                        context.write_root,
                        family=family,
                        coverage_epoch=payload.coverage_epoch,
                        change_id=document.change_id,
                        route=document.route,
                        finding_ids=tuple(str(item) for item in document.finding_ids),
                    )
                except ValueError as error:
                    raise OutputError(str(error)) from error
                scope_ref = evidence_ref(context.write_root, scope_relative)
                extra["artifacts"] = [
                    history_ref.model_dump(mode="json"),
                    scope_ref.model_dump(mode="json"),
                ]
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        **document.model_dump(
                            mode="json",
                            exclude={"rounds_used", "rounds_budget", "review_audit"},
                        ),
                        **extra,
                    },
                )
            )
        except (InputError, ValidationError) as error:
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
