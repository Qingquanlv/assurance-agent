"""Safety, policy, and override-token contracts."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from assurance_execution.contracts import ExecutionEvidenceV1
from assurance_healing.contracts.wire import (
    FrozenContract,
    HexDigest,
    StrictWireModel,
    execution_evidence_binding_digest,
    override_token_digest,
    validate_canonical_strings,
    validate_prefixed_sha256,
    validate_repo_path,
)
from assurance_intake.contracts import NonEmptyStr

Undetermined = Literal["undetermined"]


class SafetyCheck(FrozenContract):
    schema_version: NonEmptyStr
    passed: bool
    needs_review: bool
    product_code_modified: bool
    skip_or_xfail_added: bool | Undetermined
    unrelated_tests_modified: bool
    assertion_expected_value_changes_detected: bool
    high_risk_proposal_applied: bool
    bare_return_added: bool | Undetermined | None = None
    change_id: str | None = None
    source_batch_id: str | None = None
    attempt_key: str | None = None
    proposal_sha256: str | None = None
    unrelated_test_files: list[str] | None = None
    assertion_expected_value_changes: list[Any] | None = None


class CodegenFixApplySummaryV1(StrictWireModel):
    schema_version: Literal["1"]
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    claimed_modified_paths: list[StrictStr]
    intent_sha256: str
    write_set_id: StrictStr
    applied: StrictBool

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        validate_canonical_strings(self.proposal_ids, label="proposal_ids")
        validate_canonical_strings(self.claimed_modified_paths, label="claimed_modified_paths")
        for path in self.claimed_modified_paths:
            validate_repo_path(path)
        validate_prefixed_sha256(self.intent_sha256)
        if not self.write_set_id or not self.write_set_id.strip():
            raise ValueError("write_set_id must not be empty")
        if self.applied != (self.outcome == "applied"):
            raise ValueError("applied must equal (outcome == 'applied')")
        return self


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


class TestChangePolicyV1(FrozenContract):
    __test__ = False

    allowed_test_roots: tuple[NonEmptyStr, ...]
    forbidden_product_roots: tuple[NonEmptyStr, ...]
    max_files: int = Field(ge=1)
    require_approval: bool

    @model_validator(mode="after")
    def validate_roots(self) -> Self:
        if not self.allowed_test_roots:
            raise ValueError("allowed_test_roots must not be empty")
        for label, roots in (
            ("allowed_test_roots", self.allowed_test_roots),
            ("forbidden_product_roots", self.forbidden_product_roots),
        ):
            for root in roots:
                validate_repo_path(root)
            if len(set(roots)) != len(roots):
                raise ValueError(f"{label} must be unique")
        return self


class HealingOverrideTokenV1(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    action: Literal["allow_test_changes"] = "allow_test_changes"
    reason: NonEmptyStr
    policy_digest: HexDigest
    candidate_digest: HexDigest
    token_digest: HexDigest

    @model_validator(mode="after")
    def validate_token_digest(self) -> Self:
        expected = override_token_digest(
            policy_digest=self.policy_digest,
            candidate_digest=self.candidate_digest,
        )
        if self.token_digest != expected:
            raise ValueError("override token digest does not match policy and candidate")
        return self


def evidence_binding_digest(evidence: ExecutionEvidenceV1) -> str:
    return execution_evidence_binding_digest(
        baseline_tree_id=evidence.baseline_tree_id,
        mapping_digest=evidence.mapping_digest,
        receipt_digest=evidence.receipt_digest,
    )
