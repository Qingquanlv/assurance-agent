"""Versioned inputs and outputs for one generation coverage epoch."""

from __future__ import annotations

from pydantic import Field, computed_field, model_validator
from typing import Self

from graph_engine.plugin_api import FrozenModel
from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    require_same_plan,
)
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_generation.contracts.codegen import CodegenMapping
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.families import LayerName


class GeneratedFamilyV1(FrozenModel):
    family: LayerName
    coverage_epoch: int = Field(ge=0)
    plan_files: tuple[str, ...] = Field(min_length=1)
    files: tuple[GeneratedFileEntryV1, ...] = Field(min_length=1)
    mapping: CodegenMapping
    receipt: ReceiptRef
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _machine_plan_identity(self) -> Self:
        has_ref = self.case_execution_plan_ref is not None
        has_digest = self.case_execution_plan_digest is not None
        if has_ref != has_digest:
            raise ValueError("case execution plan ref and digest must be supplied together")
        if self.case_execution_plan_ref is not None:
            if self.family != "api":
                raise ValueError("case execution plans belong to the api family")
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("case execution plan ref and digest do not match")
        return self


class CompleteGenerationInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    reviewed_case: ReviewedCaseV1
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    selected_test_families: tuple[LayerName, ...] = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    families: tuple[GeneratedFamilyV1, ...] = Field(min_length=1)

    @computed_field
    @property
    def coverage_epoch_token(self) -> str:
        return str(self.coverage_epoch)

    @model_validator(mode="after")
    def _plan_matches_case(self) -> Self:
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        return self


class ResolveGenerationInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    reviewed_case: ReviewedCaseV1 | None = None
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()

    @model_validator(mode="after")
    def _plan_matches_inline_case(self) -> Self:
        if self.reviewed_case is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
        return self


class GenerationCycleResultV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    reviewed_case: ReviewedCaseV1
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    plan_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _identity_matches(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("reviewed case change_id must match generation cycle")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("reviewed case epoch must match generation cycle")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        has_ref = self.case_execution_plan_ref is not None
        has_digest = self.case_execution_plan_digest is not None
        if has_ref != has_digest:
            raise ValueError("case execution plan ref and digest must be supplied together")
        if self.case_execution_plan_ref is not None:
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("case execution plan ref and digest do not match")
            if self.case_execution_plan_ref not in self.plan_refs:
                raise ValueError("case execution plan ref must be one of the committed plan refs")
        return self


__all__ = [
    "CompleteGenerationInputV1",
    "GeneratedFamilyV1",
    "GenerationCycleResultV1",
    "ResolveGenerationInputV1",
]
