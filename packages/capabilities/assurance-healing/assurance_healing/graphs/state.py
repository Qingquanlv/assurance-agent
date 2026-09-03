from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_healing.contracts.coverage_repair import HealingRepairOutcome
from assurance_healing.contracts.status import RepairRoundKind


class HealingRepairPublicV1(FrozenModel):
    change_id: str
    effect_refs: list[dict[str, str]]
    kind: RepairRoundKind
    rounds_budget: int
    rounds_used: int
    status: HealingRepairOutcome


class HealingState(CheckpointBridgeState, total=False):
    change_id: str
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    classification: str
    fix_eligible: bool
    kind: RepairRoundKind
    rounds_budget: int
    rounds_used: int
    budgets: dict[str, int]
    activation: dict[str, str]
    current_trigger: dict[str, object]
    owner_id: str
    allowed_paths: list[str]
    allowed_roots: list[str]
    baseline_digest: str
    candidate_digest: str
    policy_digest: str
    mapping_paths: list[str]
    execution_evidence_digest: str
    brief: dict[str, object]
    status: HealingRepairOutcome
    effect_refs: list[dict[str, str]]
    human_action: Literal["approve", "reject"]
    attempt_failure: dict[str, object]


__all__ = ["HealingRepairPublicV1", "HealingState"]
