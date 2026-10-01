"""Case-repair op input and output."""

from __future__ import annotations

from pydantic import field_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.domain.artifacts import ArtifactDigestV1
from assurance_intake.domain.case_delta import CaseDeltaInputV1
from assurance_intake.domain.review_repair import ReviewRepairContractV1


class CaseRepairInputV1(CaseDeltaInputV1):
    review_repair: ReviewRepairContractV1 | None = None


class CaseRepairOutputV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...]
    review_repair: ReviewRepairContractV1

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[ArtifactDigestV1, ...]) -> tuple[ArtifactDigestV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("case-repair artifact paths must be sorted and unique")
        return value
