"""Case-design op input and output."""

from __future__ import annotations

from pydantic import Field, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.agent import ArtifactDigestV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.case_delta import CaseDeltaInputV1, fallback_preparation_refs


class CaseDesignInputV1(CaseDeltaInputV1):
    coverage_epoch: int = Field(default=0, ge=0)
    rework_ref: EvidenceArtifactRefV1 | None = None
    artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="before")
    @classmethod
    def _artifacts_fill_preparation(cls, data: object) -> object:
        return fallback_preparation_refs(data)


class CaseDesignOutputV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...] = ()
