"""Strict dormant wire artifacts for the API/E2E codegen healing boundary."""

from typing import Literal

from pydantic import StrictBool, StrictInt, StrictStr, model_validator

from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.artifacts.models.generated_files import (
    _validate_canonical_strings,
    _validate_prefixed_sha256,
    _validate_repo_path,
)


class CodegenFixApplyIntentV1(StrictWireModel):
    schema_version: Literal["1"]
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    reason: StrictStr | None = None
    claimed_modified_paths: list[StrictStr]

    @model_validator(mode="after")
    def validate_outcome(self) -> "CodegenFixApplyIntentV1":
        _validate_canonical_strings(self.proposal_ids, label="proposal_ids")
        _validate_canonical_strings(self.claimed_modified_paths, label="claimed_modified_paths")
        for path in self.claimed_modified_paths:
            _validate_repo_path(path)
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
    def validate_receipt(self) -> "FixerProposalApprovalReceiptV1":
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
            _validate_prefixed_sha256(digest)
        _validate_canonical_strings(self.targets, label="targets")
        _validate_canonical_strings(self.paths, label="paths")
        if not self.targets or not self.paths:
            raise ValueError("targets and paths must be non-empty")
        for path in self.paths:
            _validate_repo_path(path)
        return self


class FixerAuthorityPathV1(StrictWireModel):
    repo_path: StrictStr
    disposition: Literal["generated", "updated", "reused"]
    content_sha256: str

    @model_validator(mode="after")
    def validate_path(self) -> "FixerAuthorityPathV1":
        _validate_repo_path(self.repo_path)
        _validate_prefixed_sha256(self.content_sha256)
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
    def validate_target(self) -> "FixerAuthorityTargetV1":
        _validate_canonical_strings([path.repo_path for path in self.paths], label="paths.repo_path")
        for digest in (self.generated_files_sha256, self.summary_sha256):
            if digest is not None:
                _validate_prefixed_sha256(digest)
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
    def validate_authority(self) -> "FixerAuthorityV1":
        _validate_canonical_strings([target.target for target in self.targets], label="targets.target")
        return self


class CodegenFixApplySummaryV1(StrictWireModel):
    schema_version: Literal["1"]
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    claimed_modified_paths: list[StrictStr]
    intent_sha256: str
    write_set_id: StrictStr

    @model_validator(mode="after")
    def validate_summary(self) -> "CodegenFixApplySummaryV1":
        _validate_canonical_strings(self.proposal_ids, label="proposal_ids")
        _validate_canonical_strings(self.claimed_modified_paths, label="claimed_modified_paths")
        for path in self.claimed_modified_paths:
            _validate_repo_path(path)
        _validate_prefixed_sha256(self.intent_sha256)
        return self


class ApiCodegenFixApplySummaryV1(CodegenFixApplySummaryV1):
    target: Literal["api"]


class E2eCodegenFixApplySummaryV1(CodegenFixApplySummaryV1):
    target: Literal["e2e"]


class CodegenFixerSafetyCheckV1(StrictWireModel):
    schema_version: Literal["1"]
    passed: StrictBool
    needs_review: StrictBool
    product_code_modified: StrictBool
    skip_or_xfail_added: StrictBool
    unrelated_tests_modified: StrictBool
    assertion_expected_value_changes_detected: StrictBool
    high_risk_proposal_applied: StrictBool
    applied_proposal_count: StrictInt


class ApiCodegenFixerSafetyCheckV1(CodegenFixerSafetyCheckV1):
    target: Literal["api"]


class E2eCodegenFixerSafetyCheckV1(CodegenFixerSafetyCheckV1):
    target: Literal["e2e"]


class FixerSafetyCheckV1(StrictWireModel):
    schema_version: Literal["1"]
    passed: StrictBool
    needs_review: StrictBool
    active_targets: list[Literal["api", "e2e"]]
    target_safety_sha256: list[str]

    @model_validator(mode="after")
    def validate_aggregate(self) -> "FixerSafetyCheckV1":
        _validate_canonical_strings(self.active_targets, label="active_targets")
        _validate_canonical_strings(self.target_safety_sha256, label="target_safety_sha256")
        if not self.active_targets or len(self.active_targets) != len(self.target_safety_sha256):
            raise ValueError("aggregate safety must bind one digest for every active target")
        for digest in self.target_safety_sha256:
            _validate_prefixed_sha256(digest)
        return self


__all__ = [
    "ApiCodegenFixApplyIntentV1",
    "ApiCodegenFixApplySummaryV1",
    "ApiCodegenFixerSafetyCheckV1",
    "CodegenFixApplyIntentV1",
    "CodegenFixApplySummaryV1",
    "CodegenFixerSafetyCheckV1",
    "E2eCodegenFixApplyIntentV1",
    "E2eCodegenFixApplySummaryV1",
    "E2eCodegenFixerSafetyCheckV1",
    "FixerAuthorityPathV1",
    "FixerAuthorityTargetV1",
    "FixerAuthorityV1",
    "FixerProposalApprovalReceiptV1",
    "FixerSafetyCheckV1",
]
