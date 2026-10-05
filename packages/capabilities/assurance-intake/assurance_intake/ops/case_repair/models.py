"""Case-repair op input and output."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.agent import ArtifactDigestV1, canonical_relative_paths
from assurance_intake.contracts.common import SHA256_PATTERN
from assurance_intake.contracts.review import ReviewRepairActionV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.case_delta import CaseDeltaInputV1, refresh_case_attempt_input


class ReviewRepairContractV1(FrozenModel):
    review_path: str = Field(min_length=1)
    review_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_file_digests: dict[str, str] = Field(min_length=1)
    baseline_case_documents: FrozenJSONValue = Field(default_factory=dict)
    actions: tuple[ReviewRepairActionV1, ...] = Field(min_length=1)

    @field_validator("review_path")
    @classmethod
    def _review_path(cls, value: str) -> str:
        return canonical_relative_paths((value,))[0]

    @field_validator("baseline_file_digests")
    @classmethod
    def _baseline_file_digests(cls, value: dict[str, str]) -> dict[str, str]:
        canonical = canonical_relative_paths(tuple(value))
        if tuple(value) != canonical:
            raise ValueError("baseline_file_digests keys must be sorted canonical paths")
        if any(re.fullmatch(SHA256_PATTERN, digest) is None for digest in value.values()):
            raise ValueError("baseline_file_digests values must be sha256 digests")
        return value

    @model_validator(mode="after")
    def _actions_match_baseline(self) -> ReviewRepairContractV1:
        targets = {action.artifact for action in self.actions}
        missing = targets.difference(self.baseline_file_digests)
        if missing:
            raise ValueError(f"review repair targets are missing baseline files: {sorted(missing)}")
        identities = [(action.finding_id, action.artifact, action.case_id) for action in self.actions]
        if len(identities) != len(set(identities)):
            raise ValueError("review repair actions must be unique")
        return self


class CaseRepairInputV1(CaseDeltaInputV1):
    coverage_epoch: int = Field(default=0, ge=0)
    review_repair: ReviewRepairContractV1 | None = None
    case_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    marker_ref: EvidenceArtifactRefV1 | None = None
    proposal_ref: EvidenceArtifactRefV1 | None = None
    matrix_ref: EvidenceArtifactRefV1 | None = None
    artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    rework_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="before")
    @classmethod
    def _refresh_attempt_input(cls, data: object) -> object:
        return refresh_case_attempt_input(data)


class CaseRepairOutputV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...]
    review_repair: ReviewRepairContractV1
