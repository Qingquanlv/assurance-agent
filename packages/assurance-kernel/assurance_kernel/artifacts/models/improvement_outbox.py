"""Durable self-contained Improvement reconcile outbox artifact."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_kernel.artifacts.models.retro_v3 import ImprovementCandidateV3, RetroContextV3


class ImprovementOutboxEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context: RetroContextV3
    candidate: ImprovementCandidateV3
    pipeline_failure: RetroPipelineFailure | None = None

    @model_validator(mode="after")
    def validate_embedded_digests(self) -> Self:
        if self.retro_id != self.context.retro_id:
            raise ValueError("outbox retro_id must match embedded context")
        if self.candidate_sha256 != sha256_bytes(canonical_json_bytes(self.candidate)):
            raise ValueError("outbox candidate_sha256 mismatch")
        if self.context_sha256 != sha256_bytes(canonical_json_bytes(self.context)):
            raise ValueError("outbox context_sha256 mismatch")
        if self.pipeline_failure is not None and self.pipeline_failure.retro_id != self.retro_id:
            raise ValueError("outbox pipeline failure retro_id mismatch")
        return self


__all__ = ["ImprovementOutboxEntry"]
