"""Full product flow."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from graph_engine.flow import Flow, ledger, ledger_receipt
from graph_engine.flow.sources import InputSource

from assurance_intake.contracts.plan import TestFamilyPolicyV1
from assurance_intake.handoff import PLAN, REVIEWED_CASE
from assurance_quality.ops.inspect import op as inspect_op
from assurance_product.graphs.entrypoints import _PREPARE_INPUTS, _entrypoint_model
from assurance_product.graphs.execute_tail import build_execute_tail_flow
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS

_SURFACE = TASK_ATTEMPT_CONTRACTS["surface-baseline"]
_PLAN_REF = ledger(PLAN, many=False)
_REWORK_HANDOFF = ledger(inspect_op.artifact("coverage-rework-handoff"), many=False)
_REVIEWED_CASE_REF = ledger(REVIEWED_CASE, many=False)
_INSPECT_RECEIPT = ledger_receipt(inspect_op.artifact("inspection-outcome"))
_UI_REF = ledger(_SURFACE.artifact("ui-exploration"), many=False)
_API_REF = ledger(_SURFACE.artifact("api-discovery"), many=False)


def _full_input() -> type[BaseModel]:
    class FullRootInput(_entrypoint_model("full")):
        family_policy: TestFamilyPolicyV1

    FullRootInput.__name__ = "ProductInput_full_root"
    return FullRootInput


def build_full_flow(bundles: object) -> Flow:
    typed = cast(Any, bundles)
    surface = typed.quality.surface_baseline
    prepare = typed.intake.prepare
    init = typed.generation.init_runtime
    case = typed.intake.case
    rework = typed.intake.coverage_rework
    tail = build_execute_tail_flow(bundles)
    flow = Flow(
        "full",
        input=_full_input(),
        outcomes=("achieved", "not_achieved"),
        public={"achieved": "completed", "not_achieved": "failed"},
    )
    flow.subflow(
        "surface",
        surface,
        routes={"ready": "prepare", "not_ready": "not_achieved", "failed": "not_achieved"},
    )
    flow.subflow(
        "prepare",
        prepare,
        routes={"prepared": "init", "failed": "not_achieved"},
        inputs=cast(dict[str, InputSource], dict(_PREPARE_INPUTS)),
    )
    flow.subflow("init", init, routes={"completed": "case", "failed": "not_achieved"})
    with flow.loop("coverage", budget="budgets.coverage_rounds", on_exhausted="not_achieved") as coverage:
        flow.subflow(
            "case",
            case,
            inputs={
                "coverage_epoch": coverage.round,
                "plan_ref": _PLAN_REF,
                "plan_digest": "plan_digest",
                "ui_exploration_ref": _UI_REF,
                "api_discovery_ref": _API_REF,
            },
            routes={
                "reviewed": "tail",
                "rejected": "not_achieved",
                "exhausted": "not_achieved",
                "failed": "not_achieved",
            },
        )
        flow.subflow(
            "tail",
            tail,
            inputs={
                "coverage_epoch": coverage.round,
                "source_artifacts": "artifacts",
                "timeout_seconds": "execution_timeout_seconds",
                "plan_ref": _PLAN_REF,
                "ui_exploration_ref": _UI_REF,
                "api_discovery_ref": _API_REF,
                "reviewed_refs": "reviewed_refs",
            },
            routes={
                "reported": "achieved",
                "coverage_insufficient": coverage.next("coverage-rework"),
                "diagnostic": "not_achieved",
                "needs_human": "not_achieved",
                "blocked": "not_achieved",
            },
        )
        flow.subflow(
            "coverage-rework",
            rework,
            inputs={
                "coverage_epoch": coverage.round,
                "handoff_ref": _REWORK_HANDOFF,
                "reviewed_case_ref": _REVIEWED_CASE_REF,
                "inspect_receipt": _INSPECT_RECEIPT,
            },
            routes={"done": "case", "failed": "not_achieved"},
        )
    return flow


__all__ = ["build_full_flow"]
