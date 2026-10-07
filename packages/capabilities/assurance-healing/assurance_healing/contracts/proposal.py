"""Fix-proposal and fixer-authority contracts."""

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
RepairRoundKind = Literal["failure", "coverage"]


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
