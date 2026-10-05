"""Prepare: intake, explore, then resolve the plan."""

from __future__ import annotations


from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.plan import TestFamilyPolicyV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.ops.explore import op as explore
from assurance_intake.ops.intake import op as intake
from assurance_intake.ops.resolve_plan import op as resolve_plan


class PrepareFlowInput(FrozenModel):
    """Parent fields the prepare steps read. Derived plan inputs stay inside resolve-plan."""

    change_id: str
    requirement: str
    candidate_test_families: tuple[str, ...]
    capability_leafs: tuple[str, ...]
    allowed_artifact_paths: tuple[str, ...]
    budgets: dict[str, int]
    family_policy: TestFamilyPolicyV1
    product_policy: dict[str, str]
    capability_catalog: dict[str, str]
    data_knowledge: dict[str, str]
    artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    coverage_epoch: int = 0
    healing_rounds_used: int = 0


def build_prepare_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("prepare", input=PrepareFlowInput, outcomes=("prepared", "failed"))
    shared = {"artifact_paths": "allowed_artifact_paths"}
    flow.step("intake", intake, on_failure="failed", then="explore", inputs=shared)
    flow.step("explore", explore, on_failure="failed", then="resolve-plan", inputs=shared)
    flow.step("resolve-plan", resolve_plan, on_failure="failed", then="prepared")
    flow.control(
        "resolve-plan",
        plan_digest="plan.plan_digest",
        selected_test_families="plan.selected_test_families",
        preparation_refs="preparation_refs",
    )
    return flow.bind(context)


__all__ = ["PrepareFlowInput", "build_prepare_graph"]
