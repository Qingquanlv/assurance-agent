"""Strict dormant wire artifacts for the API/E2E codegen healing boundary.

Task 1 (Minimal now) ships only the exactly-specified intent and approval
receipt models. FixerAuthority, apply-summary, and safety shapes are deferred
to Task 8.
"""

from typing import Literal

from pydantic import StrictStr, model_validator

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


__all__ = [
    "ApiCodegenFixApplyIntentV1",
    "CodegenFixApplyIntentV1",
    "E2eCodegenFixApplyIntentV1",
    "FixerProposalApprovalReceiptV1",
]
