"""Data-only healing durable-effect intent and receipt contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from assurance_healing.contracts.wire import (
    FrozenContract,
    HexDigest,
    heal_apply_intent_digest,
    validate_repo_path,
)
from assurance_intake.contracts import NonEmptyStr


class HealingAllocationIntentV2(FrozenContract):
    schema_version: Literal["2"] = "2"
    episode_id: NonEmptyStr
    attempt_id: NonEmptyStr
    attempt_number: int = Field(ge=1)
    operation_id: NonEmptyStr
    change_id: NonEmptyStr
    owner_id: NonEmptyStr
    source_batch_id: NonEmptyStr
    entry_batch_id: NonEmptyStr
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    execution_evidence_digest: HexDigest
    baseline_embedded: bool


class HealingAllocationReceiptV2(FrozenContract):
    schema_version: Literal["2"] = "2"
    operation_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    settlement_key: HexDigest
    episode_id: NonEmptyStr
    attempt_id: NonEmptyStr
    attempt_number: int = Field(ge=1)
    change_id: NonEmptyStr
    owner_id: NonEmptyStr
    source_batch_id: NonEmptyStr
    entry_batch_id: NonEmptyStr
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    execution_evidence_digest: HexDigest
    baseline_embedded: bool

    @model_validator(mode="after")
    def validate_idempotency_key(self) -> Self:
        if self.idempotency_key != self.operation_id:
            raise ValueError("idempotency key does not match operation_id")
        return self


class ProposalApprovedIntentV1(FrozenContract):
    schema_version: Literal["1"] = "1"
    approval_id: NonEmptyStr
    change_id: NonEmptyStr
    owner_id: NonEmptyStr
    root_invocation_id: NonEmptyStr
    interrupt_task_id: NonEmptyStr
    source_gate_attempt_id: NonEmptyStr
    source_tree_id: NonEmptyStr
    target_tree_id: NonEmptyStr
    proposal_digest: HexDigest
    fixer_authority_digest: HexDigest
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    targets: tuple[Literal["api", "e2e"], ...]
    paths: tuple[NonEmptyStr, ...]
    action: Literal["approve_and_apply"] = "approve_and_apply"

    @model_validator(mode="after")
    def validate_targets_and_paths(self) -> Self:
        if not self.targets or not self.paths:
            raise ValueError("targets and paths must be non-empty")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be unique")
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("paths must be unique")
        for path in self.paths:
            validate_repo_path(path)
        return self


class ProposalApprovedReceiptV1(ProposalApprovedIntentV1):
    idempotency_key: NonEmptyStr
    settlement_key: HexDigest

    @model_validator(mode="after")
    def validate_idempotency_key(self) -> Self:
        if self.idempotency_key != self.approval_id:
            raise ValueError("idempotency key does not match approval_id")
        return self


class HealApplyIntentV2(FrozenContract):
    schema_version: Literal["2"] = "2"
    record_key: NonEmptyStr
    change_id: NonEmptyStr
    owner_id: NonEmptyStr
    target: Literal["api", "e2e"]
    entry_batch_id: NonEmptyStr
    outcome: Literal["applied", "no_op", "skipped"]
    candidate_digest: HexDigest
    baseline_digest: HexDigest
    policy_digest: HexDigest
    write_set_id: NonEmptyStr
    proposal_ids: tuple[NonEmptyStr, ...]
    claimed_modified_paths: tuple[NonEmptyStr, ...]
    safety_payload_digest: HexDigest

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        for path in self.claimed_modified_paths:
            validate_repo_path(path)
        return self


class HealApplyReceiptV2(HealApplyIntentV2):
    idempotency_key: NonEmptyStr
    intent_digest: HexDigest
    settlement_key: HexDigest

    @model_validator(mode="after")
    def validate_idempotency_key(self) -> Self:
        if self.idempotency_key != self.record_key:
            raise ValueError("idempotency key does not match record_key")
        expected = heal_apply_intent_digest(self.model_dump(mode="json"))
        if self.intent_digest != expected:
            raise ValueError("intent digest does not match intent")
        return self
