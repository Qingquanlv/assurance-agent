"""Assess: materialize the locked inputs, then route on the inspection disposition."""

from __future__ import annotations

from pydantic import Field

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow, ledger, ledger_receipt
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS as EXECUTION_TASKS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_intake.contracts import PolicyResourceV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import InspectionDisposition
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_quality.ops.inspect import op as inspect_op

_MATERIALIZE = TASK_ATTEMPT_CONTRACTS["materialize-assessment-inputs"]
_GENERATION_CYCLE = GENERATION_TASKS["publish-cycle"].artifact("cycle")
_EXECUTION_CYCLE = EXECUTION_TASKS["execute"].artifact("cycle")
_LEDGER = (_GENERATION_CYCLE, _EXECUTION_CYCLE)
_DISPOSITIONS: tuple[InspectionDisposition, ...] = (
    "satisfied",
    "coverage_insufficient",
    "repairable_execution_failure",
    "analysis_required",
    "needs_human",
    "blocked",
)


class AssessFlowInput(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    product_policy: PolicyResourceV1
    reviewed_case_ref: EvidenceArtifactRefV1
    repair_round: int = Field(default=0, ge=0)
    fact_baseline_ref: EvidenceArtifactRefV1 | None = None
    healing_ref: EvidenceArtifactRefV1 | None = None
    issue_ref: EvidenceArtifactRefV1 | None = None


def build_assess_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "quality",
        input=AssessFlowInput,
        outcomes=(*_DISPOSITIONS, "failed"),
        ledger_inputs=_LEDGER,
    )
    flow.step(
        "materialize-assessment-inputs",
        _MATERIALIZE,
        then="inspect",
        on_failure="failed",
        inputs={
            "generation_ref": ledger(_GENERATION_CYCLE, many=False),
            "execution_ref": ledger(_EXECUTION_CYCLE, many=False),
            "execution_receipt": ledger_receipt(_EXECUTION_CYCLE),
        },
    )
    flow.step(
        "inspect",
        inspect_op,
        on_failure="failed",
        route_on="disposition",
        routes={name: name for name in _DISPOSITIONS},
        inputs={
            "artifact_paths": "allowed_artifact_paths",
            "assessment_ref": ledger(_MATERIALIZE.artifact("assessment"), many=False),
            "generation_ref": ledger(_GENERATION_CYCLE, many=False),
            "execution_ref": ledger(_EXECUTION_CYCLE, many=False),
            "execution_receipt": ledger_receipt(_EXECUTION_CYCLE),
        },
    )
    return flow.bind(context)


__all__ = ["AssessFlowInput", "build_assess_graph"]
