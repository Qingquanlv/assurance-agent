"""Case: design, review, and repair until the case is reviewed or the budget ends."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import TestFamily
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.handoff import PLAN, REWORK_CONTEXT
from assurance_intake.ops.case_design import op as case_design
from assurance_intake.ops.case_repair import op as case_repair
from assurance_intake.ops.case_review import op as case_review


class CaseFlowInput(FrozenModel):
    """Parent fields the case steps read. Loop rounds and ledger refs stay out of this model."""

    change_id: str
    capability_leafs: tuple[str, ...]
    allowed_artifact_paths: tuple[str, ...]
    budgets: dict[str, int]
    plan_digest: str
    plan_ref: EvidenceArtifactRefV1
    selected_test_families: tuple[TestFamily, ...] = ()
    case_delta_paths: tuple[str, ...] = ()
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    coverage_epoch: int = Field(default=0, ge=0)
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None


HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def build_case_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "case",
        input=CaseFlowInput,
        outcomes=("reviewed", "rejected", "exhausted", "failed"),
        ledger_inputs=(PLAN, REWORK_CONTEXT),
    )
    shared = {"artifact_paths": "allowed_artifact_paths"}
    with flow.loop("review", budget="budgets.review_rounds", on_exhausted="exhausted") as review:
        flow.step(
            "case-design",
            case_design,
            on_failure="failed",
            then="case-review",
            inputs=shared,
        )
        flow.step(
            "case-review",
            case_review,
            inputs={**shared, "review_round": review.round},
            route_on="public_outcome",
            routes={
                "pass": "reviewed",
                "reject": "rejected",
                "needs_human": "human-review",
                "needs_fix": review.next("case-repair"),
            },
            on_failure="exhausted",
        )
        flow.step(
            "case-repair",
            case_repair,
            on_failure="exhausted",
            then="case-review",
            inputs=shared,
        )
        flow.gate(
            "human-review",
            decision=HumanReviewDecision,
            routes={
                "approve": "reviewed",
                "reject": "rejected",
                "request_rework": review.next("case-design"),
            },
        )
    flow.control("case-review", reviewed_refs="reviewed_case.preparation_refs")
    return flow.bind(context)


__all__ = [
    "HUMAN_REVIEW_ACTIONS",
    "CaseFlowInput",
    "HumanReviewDecision",
    "build_case_graph",
]
