"""Fact baseline: one attempt. The coverage epoch is an input, so each round is a new attempt."""

from __future__ import annotations


from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.ops.fact_baseline import op as fact_baseline


class FactFlowInput(FrozenModel):
    change_id: str
    coverage_epoch: int = 0
    plan_digest: str
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    reviewed_case_ref: EvidenceArtifactRefV1


def build_fact_baseline_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("fact-baseline", input=FactFlowInput, outcomes=("done", "failed"))
    flow.step(
        "fact-baseline",
        fact_baseline,
        on_failure="failed",
        then="done",
        inputs={"artifact_paths": "allowed_artifact_paths"},
    )
    return flow.bind(context)


__all__ = ["build_fact_baseline_graph"]
