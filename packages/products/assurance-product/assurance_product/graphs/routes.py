from __future__ import annotations

from collections.abc import Callable, Mapping

from assurance_execution.contracts.workflow import (
    ExecutionCycleResultV1,
    VerifiedExecutionCycleResultV1,
    VerifiedGenerationDefectCycleV1,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1
from graph_engine.stategraph.routing import select_exclusive_route

_BLOCKED = "blocked"
GenerationDefectAuthenticator = Callable[[Mapping[str, object], VerifiedGenerationDefectCycleV1], None]


def prepare_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"prepared": "prepared" if state.get("status") == "prepared" else None}


def execute_named_matches(
    state: Mapping[str, object],
    *,
    authenticate_generation_defect: GenerationDefectAuthenticator | None = None,
) -> dict[str, str | None]:
    if state.get("attempt_failure"):
        return {"quality": None, "repair": None}
    try:
        raw = state.get("execution_result")
        if state.get("validation_profile") in {"api_db.v1", "api_db_trace.v1"}:
            try:
                defect_cycle = VerifiedGenerationDefectCycleV1.model_validate(raw)
            except (TypeError, ValueError):
                defect_cycle = None
            if defect_cycle is not None:
                defect = defect_cycle.attempt.defect
                generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
                declared_defect = state.get("generation_defect")
                declared_authority = state.get("generation_defect_authority_ref")
                if authenticate_generation_defect is None:
                    return {"quality": None, "repair": None}
                authenticate_generation_defect(state, defect_cycle)
                eligible = (
                    defect.generation == generation
                    and defect.validation_profile == state.get("validation_profile")
                    and defect.generation.change_id == state.get("change_id")
                    and defect.generation.coverage_epoch == state.get("coverage_epoch", 0)
                    and (
                        declared_defect is not None
                        and VerifiedGenerationDefectCycleV1.model_validate(declared_defect) == defect_cycle
                    )
                    and (
                        declared_authority is not None
                        and declared_authority
                        == defect_cycle.attempt.authority_receipt.model_dump(mode="json")
                    )
                )
                return {"quality": None, "repair": "repair" if eligible else None}
            result = VerifiedExecutionCycleResultV1.model_validate(raw)
        else:
            result = ExecutionCycleResultV1.model_validate(raw)
    except (TypeError, ValueError):
        return {"quality": None, "repair": None}
    epoch = state.get("coverage_epoch", 0)
    return {
        "quality": "quality"
        if (
            result.change_id == state.get("change_id")
            and result.coverage_epoch == epoch
            and (
                isinstance(result, VerifiedExecutionCycleResultV1) or result.final_status in {"PASS", "FAIL"}
            )
        )
        else None,
        "repair": None,
    }


def run_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"quality": execute_named_matches(state)["quality"]}


def quality_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    disposition: object = None
    if not state.get("attempt_failure"):
        try:
            inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
            disposition = inspection.disposition
        except (TypeError, ValueError):
            pass
    return {
        "quality-report": "quality-report" if disposition == "satisfied" else None,
        "coverage-insufficient": (
            "coverage-insufficient" if disposition == "coverage_insufficient" else None
        ),
        "fix-proposal": ("fix-proposal" if disposition == "repairable_execution_failure" else None),
        "needs-human": "needs-human" if disposition == "needs_human" else None,
        "blocked": "blocked" if disposition == "blocked" else None,
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


def route_execute(
    state: Mapping[str, object],
    *,
    authenticate_generation_defect: GenerationDefectAuthenticator | None = None,
) -> str:
    return select_exclusive_route(
        execute_named_matches(
            state,
            authenticate_generation_defect=authenticate_generation_defect,
        ),
        otherwise=_BLOCKED,
    )


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
    "GenerationDefectAuthenticator",
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
