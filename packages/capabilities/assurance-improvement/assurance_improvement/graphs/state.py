from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState


def _replace_named_analysis(existing: object, incoming: object) -> object:
    if incoming is None:
        return existing
    return incoming


def replace_eval_analysis(existing: object, incoming: object) -> object:
    return _replace_named_analysis(existing, incoming)


def replace_issue_analysis(existing: object, incoming: object) -> object:
    return _replace_named_analysis(existing, incoming)


def replace_workflow_analysis(existing: object, incoming: object) -> object:
    return _replace_named_analysis(existing, incoming)


class ImprovementState(CheckpointBridgeState, total=False):
    change_id: str
    retro_id: str
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    artifact_paths: list[str]
    owned_evidence_ids: list[str]
    evidence_refs: list[dict[str, str]]
    source_refs: list[dict[str, str]]
    lifecycle_state: str
    source_manifest: dict[str, object]
    context_digest: str
    quality_report_digest: str
    metrics_digest: str
    issue_digest: str
    subject_digest: str
    expected_improvement_version: int
    improvement_id: str
    invocation_id: str
    archive_digest: str
    locked_signal_ids: list[str]
    window: dict[str, object]
    issue_slice: dict[str, object]
    workflow_slice: dict[str, object]
    eval_slice: dict[str, object]
    context: dict[str, object]
    current: dict[str, object]
    ts: str
    candidates: list[dict[str, object]]
    eval_analysis: Annotated[dict[str, object] | None, replace_eval_analysis]
    issue_analysis: Annotated[dict[str, object] | None, replace_issue_analysis]
    workflow_analysis: Annotated[dict[str, object] | None, replace_workflow_analysis]
    ledger: dict[str, object]
    projection: dict[str, object]
    assessment: dict[str, object]
    review_id: str
    human_action: str
    decision: str
    outcome: str
    eval_run_id: str
    report_sha256: str
    staged_sha256: str
    baseline_sha256: str | None
    target_digest: str
    memory_eval: dict[str, object]
    approved_state_digest: str
    approved_version: int
    before_sha256: str
    after_sha256: str
    receipt_sha256: str
    artifact_path: str
    sha256: str
    created: bool
    reason: str
    restored_sha256: str
    archive_status: str
    receipt_refs: list[dict[str, str]]
    effect_refs: list[dict[str, str]]
    status: str
    attempt_failure: dict[str, Any]


def as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    raise TypeError("expected a mapping")


__all__ = [
    "ImprovementState",
    "as_mapping",
    "replace_eval_analysis",
    "replace_issue_analysis",
    "replace_workflow_analysis",
]
