"""Versioned output for one completed execution or rerun batch."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic.types import AwareDatetime

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan
from assurance_generation.contracts.execution_plan import ValidationProfile
from graph_engine.attempts import AttemptKey


class ExecutionCycleResultV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    executed_at: AwareDatetime
    final_status: Literal["PASS", "FAIL"]
    evidence_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _paths_match_change(self) -> Self:
        prefix = f"qa/changes/{self.change_id}/"
        if not self.evidence_ref.path.startswith(prefix):
            raise ValueError("execution evidence must belong to the current change")
        if not self.mapping_ref.path.startswith(prefix):
            raise ValueError("execution mapping must belong to the current change")
        if any(not item.path.startswith(prefix) for item in self.source_refs):
            raise ValueError("execution sources must belong to the current change")
        return self


class ExecutionCycleInputV1(FrozenModel):
    generation: GenerationCycleResultV1
    repair_round: int = Field(default=0, ge=0)


class VerifiedExecutionCycleResultV1(FrozenModel):
    """Committed verified execution cycle kept separate from legacy PASS/FAIL."""

    schema_version: Literal["1"] = "1"
    validation_profile: ValidationProfile
    change_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    reviewed_case: ReviewedCaseV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    case_execution_plan_ref: EvidenceArtifactRefV1
    case_execution_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    attempt_key: AttemptKey
    batch_id: str = Field(min_length=1)
    executed_at: AwareDatetime
    completion_status: Literal["collected", "incomplete"]
    mapping_ref: EvidenceArtifactRefV1
    mapping_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_ref: EvidenceArtifactRefV1
    evidence_ref: EvidenceArtifactRefV1
    execution_index_ref: EvidenceArtifactRefV1
    execution_authority_ref: EvidenceArtifactRefV1
    raw_evidence_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _closed_verified_cycle(self) -> Self:
        if (
            self.reviewed_case.change_id != self.change_id
            or self.reviewed_case.coverage_epoch != self.coverage_epoch
        ):
            raise ValueError("verified cycle ReviewedCase identity does not match")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
            raise ValueError("verified cycle machine plan ref and digest do not match")
        if self.mapping_ref.digest != self.mapping_digest:
            raise ValueError("verified cycle mapping ref and digest do not match")
        prefix = f"qa/changes/{self.change_id}/"
        refs = (
            self.case_execution_plan_ref,
            self.mapping_ref,
            self.manifest_ref,
            self.evidence_ref,
            self.execution_index_ref,
            self.execution_authority_ref,
            *self.raw_evidence_refs,
            *self.source_refs,
        )
        if any(not item.path.startswith(prefix) for item in refs):
            raise ValueError("verified cycle refs must belong to the current change")
        return self


__all__ = [
    "ExecutionCycleInputV1",
    "ExecutionCycleResultV1",
    "VerifiedExecutionCycleResultV1",
]
