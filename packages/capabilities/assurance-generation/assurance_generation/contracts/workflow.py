"""Versioned inputs and outputs for one generation coverage epoch."""

from __future__ import annotations

from pydantic import Field, model_validator
from typing import Self

from graph_engine.plugin_api import FrozenModel
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1


class ResolveGenerationInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    reviewed_case: ReviewedCaseV1 | None = None
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()


class GenerationCycleResultV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    plan_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _identity_matches(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("reviewed case change_id must match generation cycle")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("reviewed case epoch must match generation cycle")
        return self


__all__ = ["GenerationCycleResultV1", "ResolveGenerationInputV1"]
