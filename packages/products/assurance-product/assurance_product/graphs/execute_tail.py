"""Execute tail as a product flow."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import Field

from graph_engine.flow import Flow, const, ledger, ledger_receipt
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS as EXECUTION_TASKS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_healing.ops.apply_test_repair import op as apply_test_repair
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
from assurance_improvement.contracts.retro import RetroWindow
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.handoff import REVIEWED_CASE
from assurance_intake.ops.case_review import op as case_review
from assurance_product.models import BusinessBudgetsV1, ResourceRefV1
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS as QUALITY_TASKS
from assurance_quality.ops.fact_baseline import op as fact_baseline
from assurance_quality.ops.inspect import op as inspect_op
from assurance_quality.ops.issue_analysis import op as issue_analysis
from assurance_quality.ops.report import op as report_op

_REVIEW_HISTORY = case_review.artifact("history")
_GENERATION_CYCLE = GENERATION_TASKS["publish-cycle"].artifact("cycle")
_EXECUTION_CYCLE = EXECUTION_TASKS["execute"].artifact("cycle")
_APPLIED_REPAIR = apply_test_repair.artifact("applied-repair")
_REPORT = report_op.artifact("report")
_RECONCILE_SNAPSHOT = QUALITY_TASKS["reconcile-issues"].artifact("snapshot")
_BASELINE_REF = ledger(fact_baseline.artifact("baseline"), many=False)
_INSPECTION_REF = ledger(inspect_op.artifact("inspection-outcome"), many=False)
_ASSESSMENT_REF = ledger(QUALITY_TASKS["materialize-assessment-inputs"].artifact("assessment"), many=False)
_GENERATION_REF = ledger(_GENERATION_CYCLE, many=False)
_INSPECTION_RECEIPT = ledger_receipt(inspect_op.artifact("inspection-outcome"))
_REPORT_INPUTS = {
    "fact_baseline_ref": _BASELINE_REF,
    "artifact_paths": "allowed_artifact_paths",
    "inspection_ref": _INSPECTION_REF,
    "assessment_ref": _ASSESSMENT_REF,
    "generation_ref": _GENERATION_REF,
    "execution_ref": ledger(_EXECUTION_CYCLE, many=False),
    "execution_receipt": ledger_receipt(_EXECUTION_CYCLE),
    "inspection_receipt": _INSPECTION_RECEIPT,
}
_ISSUE_ANALYSIS_REF = ledger(issue_analysis.artifact("issue-analysis"), many=False)
_ISSUE_HANDOFF_REF = ledger(issue_analysis.artifact("issue-analysis-handoff"), many=False)
_SNAPSHOT_REF = ledger(QUALITY_TASKS["reconcile-issues"].artifact("snapshot"), many=False)
_RUNTIME_REF = ledger(
    IMPROVEMENT_TASKS["assurance.improvement.retro-runtime-snapshot"].artifact("runtime-evidence"),
    many=False,
)

_OUTCOMES = ("reported", "coverage_insufficient", "diagnostic", "needs_human", "blocked")
_ASSESS_ROUTES = {
    "satisfied": "report",
    "coverage_insufficient": "coverage_insufficient",
    "analysis_required": "issue-analyze",
    "needs_human": "needs_human",
    "blocked": "issue-analyze",
    "failed": "blocked",
}


class ExecuteTailFlowInput(FrozenModel):
    """Raw data the stages before execute already know. Wheels derive the rest."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    reviewed_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    selected_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    capability_leafs: tuple[str, ...]
    allowed_artifact_paths: tuple[str, ...] = ()
    budgets: BusinessBudgetsV1
    product_policy: ResourceRefV1
    allowed_origins: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=3600, ge=31, le=3600)
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    retro_window: RetroWindow | None = None
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None


def build_execute_tail_flow(bundles: object) -> Flow:
    """Tail topology. Compile it with a product context; do not mount it on the factory."""
    typed = cast(Any, bundles)
    quality = typed.quality
    flow = Flow(
        "execute-tail",
        input=ExecuteTailFlowInput,
        outcomes=_OUTCOMES,
        ledger_inputs=(_REVIEW_HISTORY, REVIEWED_CASE),
    )
    flow.subflow(
        "fact-baseline",
        quality.fact_baseline,
        routes={"done": "generation", "failed": "blocked"},
        inputs={"reviewed_case_ref": ledger(REVIEWED_CASE, many=False)},
    )
    flow.subflow(
        "generation",
        typed.generation.generation,
        routes={"passed": "execute", "failed": "blocked"},
        inputs={"reviewed_case_ref": ledger(REVIEWED_CASE, many=False)},
    )
    flow.subflow(
        "execute",
        typed.execution.execute,
        routes={"committed": "quality", "failed": "blocked"},
        inputs={"generation_ref": ledger(_GENERATION_CYCLE, many=False)},
    )
    with flow.loop("healing", budget="budgets.healing_rounds", on_exhausted="needs_human") as healing:
        repairable: dict[str, Any] = dict(_ASSESS_ROUTES)
        repairable["repairable_execution_failure"] = healing.next("repair")
        flow.subflow(
            "quality",
            quality.assess,
            routes=repairable,
            inputs={
                "fact_baseline_ref": _BASELINE_REF,
                "repair_round": healing.round,
                "reviewed_case_ref": ledger(REVIEWED_CASE, many=False),
            },
        )
        flow.subflow(
            "repair",
            typed.healing.repair_failure,
            routes={"applied": "rerun", "needs_review": "needs_human", "failed": "blocked"},
            inputs={
                "repair_round": healing.round,
                "issue_analysis_handoff_ref": _ISSUE_HANDOFF_REF,
                "generation_ref": _GENERATION_REF,
                "execution_ref": ledger(_EXECUTION_CYCLE, many=False),
                "execution_receipt": ledger_receipt(_EXECUTION_CYCLE),
            },
        )
        flow.subflow(
            "rerun",
            typed.execution.rerun,
            routes={"committed": "quality", "failed": "blocked"},
            inputs={
                "repair_round": healing.round,
                "generation_ref": ledger(_GENERATION_CYCLE, many=False),
                "applied_repair_ref": ledger(_APPLIED_REPAIR, many=False),
                "apply_receipt": ledger_receipt(_APPLIED_REPAIR),
            },
        )
        flow.subflow(
            "issue-analyze",
            quality.issue_analyze,
            routes={
                "fix_eligible": healing.next("repair"),
                "report_issue": "diagnostic-report",
                "unclassified": "needs_human",
                "failed": "blocked",
            },
            inputs={
                "inspection_ref": _INSPECTION_REF,
                "assessment_ref": _ASSESSMENT_REF,
                "generation_ref": _GENERATION_REF,
                "inspection_receipt": _INSPECTION_RECEIPT,
            },
        )
    _report_routes = {"reported": "reported", "diagnostic": "issue-reconcile", "failed": "blocked"}
    flow.subflow(
        "report",
        quality.report,
        routes=_report_routes,
        inputs={
            "purpose": const("normal"),
            "issue_analysis_ref": const(None),
            **_REPORT_INPUTS,
        },
    )
    flow.subflow(
        "diagnostic-report",
        quality.report,
        routes=_report_routes,
        inputs={
            "purpose": const("diagnostic"),
            "issue_analysis_ref": _ISSUE_ANALYSIS_REF,
            **_REPORT_INPUTS,
        },
    )
    flow.subflow(
        "issue-reconcile",
        quality.issue_reconcile,
        routes={"ready": "runtime-snapshot", "failed": "blocked"},
        inputs={
            "inspection_ref": _INSPECTION_REF,
            "assessment_ref": _ASSESSMENT_REF,
            "generation_ref": _GENERATION_REF,
            "inspection_receipt": _INSPECTION_RECEIPT,
            "issue_analysis_ref": _ISSUE_ANALYSIS_REF,
        },
    )
    flow.subflow(
        "runtime-snapshot",
        typed.improvement.runtime_snapshot,
        routes={"done": "retro", "failed": "blocked"},
    )
    flow.subflow(
        "retro",
        typed.improvement.retro,
        routes={"done": "diagnostic", "failed": "blocked"},
        inputs={
            "source_refs": "source_artifacts",
            "runtime_ref": _RUNTIME_REF,
            "issue_snapshot_ref": _SNAPSHOT_REF,
            "window": "retro_window",
            "preparation_refs": "preparation_refs",
            "reviewed_refs": "reviewed_refs",
            "history_refs": ledger(_REVIEW_HISTORY, many=True),
            "carried_evidence_refs": ledger(_RECONCILE_SNAPSHOT, many=True),
            "artifact_paths": "allowed_artifact_paths",
            "report_receipt": ledger_receipt(_REPORT),
        },
    )
    return flow


__all__ = ["ExecuteTailFlowInput", "build_execute_tail_flow"]
