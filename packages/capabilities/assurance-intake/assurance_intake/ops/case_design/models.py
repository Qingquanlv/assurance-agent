"""Case-design op input and output."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.agent import ArtifactDigestV1
from assurance_intake.contracts.workflow import CaseReworkContextV1
from assurance_intake.domain.case_delta import CaseDeltaInputV1


class CaseDesignInputV1(CaseDeltaInputV1):
    coverage_epoch: int = Field(default=0, ge=0)
    case_rework_context: CaseReworkContextV1 | None = None
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _rework(self) -> CaseDesignInputV1:
        if self.case_rework_context is not None:
            previous = self.case_rework_context.previous_case
            if previous.change_id != self.change_id:
                raise ValueError("case rework change_id must match case design")
            if self.coverage_epoch != previous.coverage_epoch + 1:
                raise ValueError("case rework must advance coverage_epoch exactly once")
            unauthorized = set(self.case_rework_context.target_case_paths) - set(self.case_delta_paths)
            if unauthorized:
                raise ValueError("case rework targets must be locked by case_delta_paths")
        return self


class CaseDesignOutputV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...] = ()

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[ArtifactDigestV1, ...]) -> tuple[ArtifactDigestV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("case-design artifact paths must be sorted and unique")
        return value
