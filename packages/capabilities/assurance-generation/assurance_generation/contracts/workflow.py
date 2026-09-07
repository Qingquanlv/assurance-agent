"""Versioned inputs and outputs for one generation coverage epoch."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, computed_field, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts import AttemptKey
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.codegen import CodegenMapping
from assurance_generation.contracts.execution_plan import CasePlanContextV1, ValidationProfile
from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_intake.contracts.verification import AssertionSourcesV1
from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    require_same_plan,
)


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
    case_plan_context: CasePlanContextV1 | None = None
    assertion_sources: AssertionSourcesV1 | None = None
    validation_profile: ValidationProfile | None = None

    @model_validator(mode="after")
    def _plan_matches_inline_case(self) -> Self:
        if self.reviewed_case is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
        verified_inputs = (self.case_plan_context, self.assertion_sources, self.validation_profile)
        if any(value is not None for value in verified_inputs):
            if any(value is None for value in verified_inputs):
                raise ValueError("verified plan inputs must be supplied together")
            if self.reviewed_case is None:
                raise ValueError("verified plan inputs require an inline ReviewedCase")
            assert self.case_plan_context is not None
            if self.case_plan_context.reviewed_case != self.reviewed_case:
                raise ValueError("verified plan context does not match ReviewedCase")
            if self.case_plan_context.coverage_epoch != self.coverage_epoch:
                raise ValueError("verified plan context does not match generation epoch")
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


class VerifiedGenerationDefectV1(FrozenModel):
    """A deterministic pre-dispatch defect in one accepted generated bridge."""

    schema_version: Literal["1"] = "1"
    defect_kind: Literal["missing_bridge", "invalid_bridge"]
    generation: GenerationCycleResultV1
    validation_profile: ValidationProfile
    attempt_key: AttemptKey
    repair_round: Literal[0] = 0
    case_id: str = Field(min_length=1)
    bridge_symbol: str = Field(min_length=1)
    bridge_ref: EvidenceArtifactRefV1
    observed_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _closed_bridge_defect(self) -> Self:
        if self.generation.case_execution_plan_ref is None:
            raise ValueError("verified generation defect requires a machine plan")
        if self.bridge_ref not in self.generation.source_refs:
            raise ValueError("verified generation defect bridge is outside source closure")
        if "/generated/api/files/" not in self.bridge_ref.path:
            raise ValueError("verified generation defect must identify an API bridge")
        if self.defect_kind == "missing_bridge" and self.observed_digest is not None:
            raise ValueError("missing bridge cannot carry an observed digest")
        if self.defect_kind == "invalid_bridge" and self.observed_digest is None:
            raise ValueError("invalid bridge requires its observed digest")
        return self


__all__ = [
    "CompleteGenerationInputV1",
    "GeneratedFamilyV1",
    "GenerationCycleResultV1",
    "ResolveGenerationInputV1",
    "VerifiedGenerationDefectV1",
]
