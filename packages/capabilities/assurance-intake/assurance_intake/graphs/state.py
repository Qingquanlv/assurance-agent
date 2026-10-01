from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_intake.domain.history_refs import merge_history_refs


def as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


class IntakeState(CheckpointBridgeState, total=False):
    change_id: str
    requirement: str
    candidate_test_families: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    budgets: dict[str, int]
    family_policy: dict[str, object]
    selected_test_families: list[str]
    plan_digest: str
    plan_ref: dict[str, str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    rounds_used: int
    rounds_budget: int
    decision: str
    auto_fix_allowed: bool
    human_review_required: bool
    human_action: str
    artifacts: list[dict[str, object]]
    history_refs: Annotated[list[dict[str, str]], merge_history_refs]
    status: str
    attempt_failure: dict[str, object]
    coverage_epoch: int
    preparation_refs: list[dict[str, str]]
    case_refs: list[dict[str, str]]
    case_rework_context: dict[str, object] | None
    reviewed_case: dict[str, object] | None
    case_receipt: dict[str, str] | None
    receipt: dict[str, str]
    ui_exploration_ref: dict[str, str]
    api_discovery_ref: dict[str, str]


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "failed", "reviewed_case": None, "case_receipt": None}


__all__ = ["IntakeState", "as_int", "terminal_failed"]
