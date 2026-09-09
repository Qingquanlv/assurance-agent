from __future__ import annotations

from collections.abc import Mapping

from assurance_execution.contracts.workflow import (
    ExecutionCycleResultV1,
    VerifiedExecutionCycleResultV1,
    VerifiedIncompleteExecutionV1,
)
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1
from graph_engine.stategraph.routing import select_exclusive_route

_BLOCKED = "blocked"


def prepare_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"prepared": "prepared" if state.get("status") == "prepared" else None}


def execute_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    if state.get("attempt_failure"):
        return {"quality": None}
    try:
        raw = state.get("execution_result")
        if state.get("validation_profile") in {"api_db.v1", "api_db_trace.v1"}:
            try:
                result = VerifiedIncompleteExecutionV1.model_validate(raw)
            except (TypeError, ValueError):
                result = VerifiedExecutionCycleResultV1.model_validate(raw)
        else:
            result = ExecutionCycleResultV1.model_validate(raw)
    except (TypeError, ValueError):
        return {"quality": None}
    epoch = state.get("coverage_epoch", 0)
    return {
        "quality": "quality"
        if (
            result.change_id == state.get("change_id")
            and result.coverage_epoch == epoch
            and (
                isinstance(result, (VerifiedExecutionCycleResultV1, VerifiedIncompleteExecutionV1))
                or result.final_status in {"PASS", "FAIL"}
            )
        )
        else None
    }


def run_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    try:
        if isinstance(
            VerifiedIncompleteExecutionV1.model_validate(state.get("execution_result")),
            VerifiedIncompleteExecutionV1,
        ):
            return {"quality": None}
    except (TypeError, ValueError):
        pass
    return execute_named_matches(state)


def quality_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    disposition: object = None
    inspection: InspectionOutcomeV1 | None = None
    if not state.get("attempt_failure"):
        try:
            inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
            disposition = inspection.disposition
        except (TypeError, ValueError):
            pass
    diagnostic_ready = False
    if inspection is not None and disposition in {"blocked", "analysis_required"}:
        try:
            assessment = AssessmentInputsV1.model_validate(state.get("assessment_inputs"))
            state_owned = state.get("owned_evidence_ids")
            owned = list(assessment.owned_evidence_ids)
            if state_owned is None:
                state_owned = owned
            state_digest = state.get("evidence_bundle_digest") or assessment.evidence_bundle_digest
            diagnostic_ready = (
                bool(owned)
                and assessment.change_id == inspection.change_id
                and assessment.batch_id == inspection.batch_id
                and assessment.coverage_epoch == inspection.coverage_epoch
                and isinstance(state_owned, (list, tuple))
                and list(state_owned) == owned
                and state_digest == assessment.evidence_bundle_digest
            )
        except (TypeError, ValueError):
            pass
    return {
        "quality-report": "quality-report" if disposition == "satisfied" else None,
        "coverage-insufficient": (
            "coverage-insufficient" if disposition == "coverage_insufficient" else None
        ),
        "fix-proposal": ("fix-proposal" if disposition == "repairable_execution_failure" else None),
        "needs-human": "needs-human" if disposition == "needs_human" else None,
        "diagnostic": "diagnostic" if diagnostic_ready else None,
    }


def applied_repair_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    status: object = None
    if not state.get("attempt_failure"):
        try:
            repair = AppliedTestRepairV1.model_validate(state.get("repair_result"))
            if repair.change_id == state.get("change_id") and repair.coverage_epoch == state.get(
                "coverage_epoch", 0
            ):
                status = repair.status
        except (TypeError, ValueError):
            pass
    return {
        "rerun": "rerun" if status == "applied" else None,
        "needs-human": "needs-human" if status == "needs_review" else None,
        "blocked": ("blocked" if status in {"not_eligible", "exhausted", "failed"} else None),
    }


def route_prepare(state: Mapping[str, object]) -> str:
    return select_exclusive_route(prepare_named_matches(state), otherwise="failed")


def route_execute(state: Mapping[str, object]) -> str:
    return select_exclusive_route(execute_named_matches(state), otherwise=_BLOCKED)


def route_run(state: Mapping[str, object]) -> str:
    return select_exclusive_route(run_named_matches(state), otherwise=_BLOCKED)


def route_quality(state: Mapping[str, object]) -> str:
    return select_exclusive_route(quality_named_matches(state), otherwise=_BLOCKED)


def route_applied_repair(state: Mapping[str, object]) -> str:
    return select_exclusive_route(applied_repair_named_matches(state), otherwise=_BLOCKED)


PRODUCT_EXCLUSIVE_ROUTES = {
    "prepare": route_prepare,
    "execute": route_execute,
    "run": route_run,
    "quality": route_quality,
    "fix-proposal": route_applied_repair,
}


__all__ = [
    "PRODUCT_EXCLUSIVE_ROUTES",
    "applied_repair_named_matches",
    "execute_named_matches",
    "prepare_named_matches",
    "quality_named_matches",
    "route_applied_repair",
    "route_execute",
    "route_prepare",
    "route_quality",
    "route_run",
    "run_named_matches",
]
