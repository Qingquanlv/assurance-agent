"""Private prepare/finalize and handler request models owned by assurance-healing."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts import LayerName
from assurance_healing.contracts.proposal import FixProposalSummary
from assurance_healing.contracts.wire import FrozenContract, HexDigest, validate_repo_path
from assurance_intake.contracts import EvidenceArtifactRefV1, NonEmptyStr


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def _canonical_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = _sorted_unique(values, label="path")
    for path in paths:
        validate_repo_path(path)
    return paths


class FixProposalResultItemV1(FrozenContract):
    proposal_id: NonEmptyStr
    target: LayerName
    eligible: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    needs_review: bool
    files_to_modify: tuple[NonEmptyStr, ...] = ()

    @field_validator("files_to_modify")
    @classmethod
    def _files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value) if value else ()


class FixProposalResultV1(FrozenContract):
    schema_version: NonEmptyStr
    change_id: NonEmptyStr
    summary: FixProposalSummary
    proposals: tuple[FixProposalResultItemV1, ...] = ()


class FixProposalInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: HexDigest
    plan_ref: EvidenceArtifactRefV1
    owner_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    allowed_roots: tuple[str, ...]
    baseline_digest: HexDigest
    candidate_digest: HexDigest
    policy_digest: HexDigest
    mapping_paths: tuple[str, ...]
    require_approval: bool = True
    execution_evidence_digest: HexDigest
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None
    issue_analysis_handoff_ref: EvidenceArtifactRefV1 | None = None
    coverage_epoch: int = Field(default=0, ge=0)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _analysis_belongs_to_change(self) -> FixProposalInputV1:
        if self.issue_analysis_ref is not None and self.issue_analysis_ref.path != (
            "qa/results/inspect/issue-analysis.json"
        ):
            raise ValueError("issue analysis must belong to the repaired change")
        return self

    @field_validator("capability_leafs")
    @classmethod
    def _leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("allowed_paths", "mapping_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value)

    @field_validator("allowed_roots")
    @classmethod
    def _roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        roots = _sorted_unique(value, label="allowed root")
        if not roots:
            raise ValueError("allowed_roots must be non-empty")
        return roots


class FixProposalFinalizeInputV1(FixProposalInputV1):
    agent_result: AgentRunResult
    prepare: FixProposalInputV1


class AllocateHealingInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: HexDigest
    plan_ref: EvidenceArtifactRefV1
    owner_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    source_batch_id: str = Field(min_length=1)
    entry_batch_id: str = Field(min_length=1)
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    execution_evidence_digest: HexDigest
    prior_operation_ids: tuple[str, ...] = ()


class RecordApprovalInputV1(FrozenModel):
    approval_id: str | None = None
    change_id: str = Field(min_length=1)
    plan_digest: HexDigest
    plan_ref: EvidenceArtifactRefV1
    owner_id: str = Field(min_length=1)
    root_invocation_id: str = Field(min_length=1)
    interrupt_task_id: str = Field(min_length=1)
    source_gate_attempt_id: str = Field(min_length=1)
    source_tree_id: str = Field(min_length=1)
    target_tree_id: str = Field(min_length=1)
    proposal_digest: HexDigest
    fixer_authority_digest: HexDigest
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    targets: tuple[Literal["api", "e2e"], ...]
    paths: tuple[str, ...]
    action: Literal["approve_and_apply"] = "approve_and_apply"

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value)


class RecordApplyInputV1(FrozenModel):
    record_key: str | None = None
    change_id: str = Field(min_length=1)
    plan_digest: HexDigest
    plan_ref: EvidenceArtifactRefV1
    owner_id: str = Field(min_length=1)
    target: Literal["api", "e2e"]
    entry_batch_id: str = Field(min_length=1)
    outcome: Literal["applied", "no_op", "skipped"]
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    write_set_id: str = Field(min_length=1)
    proposal_ids: tuple[str, ...]
    claimed_modified_paths: tuple[str, ...]
    safety_payload_digest: HexDigest

    @field_validator("proposal_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="proposal id")

    @field_validator("claimed_modified_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value)


__all__ = [
    "FixProposalFinalizeInputV1",
    "AllocateHealingInputV1",
    "FixProposalInputV1",
    "FixProposalResultItemV1",
    "FixProposalResultV1",
    "RecordApprovalInputV1",
    "RecordApplyInputV1",
]
