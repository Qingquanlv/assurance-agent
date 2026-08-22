"""Fix-proposal, apply-intent, and fixer-authority contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import StrictStr, model_validator

from assurance_generation.contracts import LayerName
from assurance_healing.contracts.wire import (
    FrozenContract,
    StrictWireModel,
    validate_canonical_strings,
    validate_prefixed_sha256,
    validate_repo_path,
)
from assurance_intake.contracts import NonEmptyStr

Undetermined = Literal["undetermined"]


class FixProposalSummary(FrozenContract):
    eligible_count: int


class FixProposalItem(FrozenContract):
    target: LayerName
    eligible: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    needs_review: bool


class FixProposal(FrozenContract):
    schema_version: NonEmptyStr
    summary: FixProposalSummary
    proposals: list[FixProposalItem]


class ApplySummary(FrozenContract):
    schema_version: NonEmptyStr
    target: Literal["api", "e2e"]
    applied: bool


class CodegenFixApplyIntentV1(StrictWireModel):
    schema_version: Literal["1"]
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    reason: StrictStr | None = None
    claimed_modified_paths: list[StrictStr]

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        validate_canonical_strings(self.proposal_ids, label="proposal_ids")
        validate_canonical_strings(self.claimed_modified_paths, label="claimed_modified_paths")
        for path in self.claimed_modified_paths:
            validate_repo_path(path)
        if self.outcome == "applied":
            if not self.proposal_ids:
                raise ValueError("applied intents require proposal_ids")
            if not self.claimed_modified_paths:
                raise ValueError("applied intents require claimed_modified_paths")
            if self.reason is not None:
                raise ValueError("applied intents must not provide a reason")
        else:
            if self.reason is None or not self.reason.strip():
                raise ValueError(f"{self.outcome} intents require a non-empty reason")
            if self.claimed_modified_paths:
                raise ValueError(f"{self.outcome} intents must not claim modified paths")
        return self


class ApiCodegenFixApplyIntentV1(CodegenFixApplyIntentV1):
    target: Literal["api"]


class E2eCodegenFixApplyIntentV1(CodegenFixApplyIntentV1):
    target: Literal["e2e"]


class FixerProposalApprovalReceiptV1(StrictWireModel):
    schema_version: Literal["1"]
    approval_id: str
    root_invocation_id: str
    interrupt_task_id: str
    source_gate_attempt_id: str
    source_tree_id: str
    proposal_sha256: str
    fixer_authority_sha256: str
    entry_baseline_sha256: str
    policy_sha256: str
    targets: list[Literal["api", "e2e"]]
    paths: list[StrictStr]
    action: Literal["approve_and_apply"]

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        for field_name in (
            "approval_id",
            "root_invocation_id",
            "interrupt_task_id",
            "source_gate_attempt_id",
            "source_tree_id",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} must not be empty")
        for digest in (
            self.proposal_sha256,
            self.fixer_authority_sha256,
            self.entry_baseline_sha256,
            self.policy_sha256,
        ):
            validate_prefixed_sha256(digest)
        validate_canonical_strings(self.targets, label="targets")
        validate_canonical_strings(self.paths, label="paths")
        if not self.targets or not self.paths:
            raise ValueError("targets and paths must be non-empty")
        for path in self.paths:
            validate_repo_path(path)
        return self


class FixerAuthorityPathV1(StrictWireModel):
    repo_path: StrictStr
    disposition: Literal["generated", "updated", "reused"]
    content_sha256: str

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        validate_repo_path(self.repo_path)
        validate_prefixed_sha256(self.content_sha256)
        return self


class FixerAuthorityTargetV1(StrictWireModel):
    target: Literal["api", "e2e"]
    status: Literal["ready", "unverified"]
    codegen_attempt_id: StrictStr | None = None
    generated_files_sha256: str | None = None
    summary_sha256: str | None = None
    write_set_id: StrictStr | None = None
    execution_batch_id: StrictStr | None = None
    paths: list[FixerAuthorityPathV1]

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        validate_canonical_strings([path.repo_path for path in self.paths], label="paths.repo_path")
        for digest in (self.generated_files_sha256, self.summary_sha256):
            if digest is not None:
                validate_prefixed_sha256(digest)
        if self.status == "ready" and any(
            value is None
            for value in (
                self.codegen_attempt_id,
                self.generated_files_sha256,
                self.summary_sha256,
                self.write_set_id,
                self.execution_batch_id,
            )
        ):
            raise ValueError("ready authority target requires all bound identities")
        return self


class FixerAuthorityV1(StrictWireModel):
    schema_version: Literal["1"]
    change_id: StrictStr
    targets: list[FixerAuthorityTargetV1]

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        validate_canonical_strings([target.target for target in self.targets], label="targets.target")
        return self
