"""Capability-closed four-family plan-review prepare/finalize handlers."""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import AgentBindingDataV1, AgentFinalizeInputV1
from assurance_generation.contracts.reviews import PlanReviewAuthoring, normalize_public_review_outcome
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
    evidence_ref,
    leafs_of,
    load_family_cases,
    planning_facts_for,
    review_input_images,
    plan_review_outputs,
    plan_review_input_paths,
    prepare_plan_outcome,
    persist_loop_round_history,
    resolve_family,
    validate_plan_input,
    validate_reviewed_plan,
)
from assurance_generation.operations.plan_review_policy import (
    apply_plan_review_policy,
    load_finding_scope,
    write_finding_scope,
)
from assurance_generation.operations.review_audit import (
    api_review_requirements,
    repair_api_review_audit,
    validate_api_review_audit,
)

_REVIEW_SKILL_FILES: dict[Family, str] = {
    "api": "skills/aa-api-plan-reviewer/SKILL.md",
    "e2e": "skills/aa-e2e-plan-reviewer/SKILL.md",
    "fuzz": "skills/aa-fuzz-plan-reviewer/SKILL.md",
    "performance": "skills/aa-performance-plan-reviewer/SKILL.md",
}


class PlanReviewPrepareHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            business, cases = validate_plan_input(
                request.input,
                family=family,
                workspace=context.project_root,
            )
            if business.reviewed_plan is None:
                raise InputError("reviewed_plan is required for plan review")
            validate_reviewed_plan(business, family, cases)
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
                allowed_outputs=plan_review_outputs(business.change_id, family),
                close_result_capabilities=True,
                review_input_paths=review_inputs,
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
            try:
                document = PlanReviewAuthoring.model_validate(
                    thaw_json(payload.agent_result.result_payload),
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = f"{family}-plan"
            if document.review_type != expected:
                raise OutputError(f"review_type {document.review_type!r} does not match {expected}")
            audit_inputs = None
            if family == "api":
                if document.review_audit is None:
                    raise OutputError("review_audit is required for API plan review in every round")
                paths = plan_review_input_paths(
                    context.project_root, change_id=document.change_id, family=family
                )
                images = review_input_images(context.project_root, paths)
                facts = planning_facts_for(
                    context.project_root,
                    change_id=document.change_id,
                    family=family,
                    capability_leafs=payload.capability_leafs,
                )
                cases = load_family_cases(
                    context.project_root,
                    change_id=document.change_id,
                    family=family,
                    capability_leafs=payload.capability_leafs,
                    case_paths=tuple(ref.path for ref in payload.reviewed_case.case_refs)
                    if payload.reviewed_case is not None
                    else None,
                )
                try:
                    requirements = api_review_requirements(
                        case_ids=tuple(case.case_id for case in (*cases.added, *cases.modified)),
                        facts=facts,
                        images=images,
                    )
                    document, warnings = repair_api_review_audit(
                        document,
                        requirements=requirements,
                        facts=facts,
                        images=images,
                    )
                    audit_inputs = (requirements, facts, images)
                    if warnings:
                        logging.getLogger(__name__).warning(
                            "api_review_audit_normalized (%s)", ",".join(warnings)
                        )
                except ValueError as error:
                    raise OutputError(str(error)) from error
            previous = None
            if payload.change_id is not None:
                previous = load_finding_scope(
                    context.write_root,
                    family=family,
                    coverage_epoch=payload.coverage_epoch,
                ) or load_finding_scope(
                    context.project_root,
                    family=family,
                    coverage_epoch=payload.coverage_epoch,
                )
            document = PlanReviewAuthoring.model_validate(
                apply_plan_review_policy(document.model_dump(mode="json"), previous=previous),
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            if audit_inputs is not None:
                requirements, facts, images = audit_inputs
                try:
                    validate_api_review_audit(document, requirements=requirements, facts=facts, images=images)
                except ValueError as error:
                    raise OutputError(str(error)) from error
            if family == "api" and document.review_audit is not None:
                raw_path = "qa/results/review/api-plan-review.json"
                sealed = context.write_root.joinpath(*PurePosixPath(raw_path).parts)
                sealed.parent.mkdir(parents=True, exist_ok=True)
                sealed.write_text(
                    json.dumps(document.model_dump(mode="json"), indent=2) + "\n",
                    encoding="utf-8",
                )
            extra: dict[str, object] = {
                "public_outcome": normalize_public_review_outcome(
                    document.decision,
                    document.auto_fix_allowed,
                    document.human_review_required,
                ),
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
                    f"qa/results/review/{family}-plan-review.json",
                )
                history_relative = (
                    f"qa/results/plan/{family}/reviews/epochs/"
                    f"{payload.coverage_epoch}/rounds/{payload.local_round}.json"
                )
                history_ref = persist_loop_round_history(
                    context,
                    relative=history_relative,
                    change_id=document.change_id,
                    coverage_epoch=payload.coverage_epoch,
                    loop_kind="plan_review",
                    family=family,
                    round_index=payload.local_round,
                    outcome=str(extra["public_outcome"]),
                    input_refs=input_refs,
                    source_refs=(*input_refs, review_ref),
                )
                extra["history_ref"] = history_ref.model_dump(mode="json")
                scope_relative = write_finding_scope(
                    context.write_root,
                    family=family,
                    coverage_epoch=payload.coverage_epoch,
                    change_id=document.change_id,
                    decision=document.decision,
                    finding_ids=tuple(
                        str(item["id"])
                        for item in document.findings
                        if isinstance(item, dict) and item.get("id")
                    ),
                )
                scope_ref = evidence_ref(context.write_root, scope_relative)
                extra["artifacts"] = [
                    history_ref.model_dump(mode="json"),
                    scope_ref.model_dump(mode="json"),
                ]
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        **document.model_dump(mode="json", exclude={"rounds_used", "rounds_budget"}),
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
