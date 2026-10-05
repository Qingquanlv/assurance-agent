"""Generation root: resolve inputs, run the selected family lanes, publish the cycle."""

from __future__ import annotations

from pydantic import Field

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.ledger import InputBinding, NamedWrite

from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.families import GENERATION_FAMILIES, LayerName
from assurance_generation.contracts.reviews import HumanReviewDecision
from assurance_generation.contracts.workflow import (
    GENERATION_CYCLE_PATH,
    GenerationCyclePublishedV1,
    PublishCycleInputV1,
)
from assurance_generation.feature import GenerationGraphs
from assurance_generation.graphs.init_runtime import build_init_runtime_graph
from assurance_generation.ops.api_codegen import op as api_codegen
from assurance_generation.ops.api_codegen_review import op as api_codegen_review
from assurance_generation.ops.e2e_codegen import op as e2e_codegen
from assurance_generation.ops.e2e_codegen_review import op as e2e_codegen_review
from assurance_generation.ops.fuzz_codegen import op as fuzz_codegen
from assurance_generation.ops.fuzz_codegen_review import op as fuzz_codegen_review
from assurance_generation.ops.performance_codegen import op as performance_codegen
from assurance_generation.ops.performance_codegen_review import op as performance_codegen_review
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_CODEGEN = {
    "api": api_codegen,
    "e2e": e2e_codegen,
    "fuzz": fuzz_codegen,
    "performance": performance_codegen,
}
_REVIEW = {
    "api": api_codegen_review,
    "e2e": e2e_codegen_review,
    "fuzz": fuzz_codegen_review,
    "performance": performance_codegen_review,
}
_REVIEW_BUDGET = 3


class GenerationFlowInput(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(default=0, ge=0)
    reviewed_case_ref: EvidenceArtifactRefV1 | None = None
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    selected_test_families: tuple[LayerName, ...]
    capability_leafs: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None


class LaneFlowInput(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(default=0, ge=0)
    reviewed_case_ref: EvidenceArtifactRefV1 | None = None
    capability_leafs: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None


def _family_bindings() -> tuple[InputBinding, ...]:
    bindings: list[InputBinding] = []
    for family in GENERATION_FAMILIES:
        codegen = _CODEGEN[family]
        review = _REVIEW[family]
        bindings.extend(
            (
                InputBinding(
                    ledger_key=codegen.artifact(f"{family}-files").ledger_key,
                    field=f"{family}_files",
                ),
                InputBinding(
                    ledger_key=codegen.artifact(f"{family}-summary").ledger_key,
                    field=f"{family}_summary",
                ),
                InputBinding(
                    ledger_key=review.artifact(f"{family}-review").ledger_key,
                    field=f"{family}_review",
                ),
            )
        )
    return tuple(bindings)


class PublishCycleOp:
    """Task view whose input slots are the family artifact handles."""

    def __init__(self) -> None:
        self._bindings = _family_bindings()

    @property
    def contract_id(self) -> str:
        return TASK_ATTEMPT_CONTRACTS["publish-cycle"].contract_id

    @property
    def input_model(self) -> type[PublishCycleInputV1]:
        return PublishCycleInputV1

    @property
    def output_model(self) -> type[GenerationCyclePublishedV1]:
        return GenerationCyclePublishedV1

    def ledger_namespace(self) -> str:
        return "generation"

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return (NamedWrite("cycle", GENERATION_CYCLE_PATH),)

    def input_bindings(self) -> tuple[InputBinding, ...]:
        return self._bindings


def codegen_lane(family: str) -> Flow:
    lane = Flow(family, input=LaneFlowInput, outcomes=("passed", "rejected", "exhausted", "failed"))
    with lane.loop("review", budget=_REVIEW_BUDGET, on_exhausted="exhausted") as review:
        lane.step(
            "codegen",
            _CODEGEN[family],
            inputs={"local_round": review.round, "artifact_paths": "allowed_artifact_paths"},
            then="codegen-review",
            on_failure="failed",
        )
        lane.step(
            "codegen-review",
            _REVIEW[family],
            inputs={"local_round": review.round, "artifact_paths": "allowed_artifact_paths"},
            route_on="route",
            routes={
                "codegen": "passed",
                "auto_fix": review.next("codegen"),
                "human": "human-review",
                "reject": "rejected",
            },
            on_failure="failed",
        )
        lane.gate(
            "human-review",
            decision=HumanReviewDecision,
            routes={
                "approve": "passed",
                "reject": "rejected",
                "request_rework": review.next("codegen"),
            },
        )
    return lane


_PUBLISH_CYCLE = PublishCycleOp()


def build_generation_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("generation", input=GenerationFlowInput, outcomes=("passed", "failed"))
    flow.step(
        "resolve-inputs", TASK_ATTEMPT_CONTRACTS["resolve-inputs"], then="families", on_failure="failed"
    )
    flow.parallel(
        "families",
        branches={family: codegen_lane(family) for family in GENERATION_FAMILIES},
        select="selected_test_families",
        require="passed",
        then="publish-cycle",
        on_failure="failed",
    )
    flow.step("publish-cycle", _PUBLISH_CYCLE, then="passed", on_failure="failed")
    return flow.bind(context)


def build_generation_graphs(context: CapabilityBuildContext) -> GenerationGraphs:
    return GenerationGraphs(
        generation=build_generation_graph(context),
        init_runtime=build_init_runtime_graph(context),
    )


__all__ = ["GenerationGraphs", "build_generation_graph", "build_generation_graphs", "codegen_lane"]
