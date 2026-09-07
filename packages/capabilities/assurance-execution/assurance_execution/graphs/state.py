from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState


class ExecutionPublicOutput(FrozenModel):
    batch_id: str
    execution_evidence: dict[str, object]
    execution_digest: str
    execution_semantic_node_id: Literal["execution.execute", "execution.run"]
    rounds_budget: int
    rounds_used: int
    status: Literal["failed", "passed", "collected", "incomplete", "generation_defect"]


class ExecutionState(CheckpointBridgeState, total=False):
    coverage_epoch: int
    repair_round: int
    generation_result: dict[str, object]
    validation_profile: str | None
    verification_config_digest: str | None
    verification: dict[str, object]
    execution_result: dict[str, object]
    generation_defect: dict[str, object]
    generation_defect_authority_ref: dict[str, str]
    change_id: str
    plan_digest: str
    plan_ref: dict[str, str]
    batch_id: str
    selected_test_families: list[str]
    capability_leafs: list[str]
    case_ids: list[str]
    artifact_paths: list[str]
    mapping: dict[str, object]
    selected_targets: dict[str, bool]
    baseline_tree_id: str
    runner_profile_digest: str
    rounds_budget: int
    rounds_used: int
    activation: dict[str, str]
    status: Literal["failed", "passed", "collected", "incomplete", "generation_defect"]
    execution_evidence: dict[str, object]
    execution_digest: str
    execution_semantic_node_id: Literal["execution.execute", "execution.run"]
    attempt_failure: dict[str, object]


__all__ = ["ExecutionPublicOutput", "ExecutionState"]
