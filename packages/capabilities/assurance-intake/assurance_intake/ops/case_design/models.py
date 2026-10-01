"""Case-design op input and output."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import CaseReworkContextV1
from assurance_intake.domain.artifacts import ArtifactDigestV1
from assurance_intake.domain.case_delta import CaseDeltaInputV1


class CaseDesignInputV1(CaseDeltaInputV1):
    coverage_epoch: int = Field(default=0, ge=0)
    case_rework_context: CaseReworkContextV1 | None = None
    validation_attempt: Literal[0, 1] = 0
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _rework_and_validation_attempt(self) -> CaseDesignInputV1:
        if self.case_rework_context is not None:
            previous = self.case_rework_context.previous_case
            if previous.change_id != self.change_id:
                raise ValueError("case rework change_id must match case design")
            if self.coverage_epoch != previous.coverage_epoch + 1:
                raise ValueError("case rework must advance coverage_epoch exactly once")
            unauthorized = set(self.case_rework_context.target_case_paths) - set(self.case_delta_paths)
            if unauthorized:
                raise ValueError("case rework targets must be locked by case_delta_paths")
        if self.validation_attempt == 0 and self.validation_error is not None:
            raise ValueError("validation_error is allowed only for the validation repair attempt")
        if self.validation_attempt == 1 and self.validation_error is None:
            raise ValueError("validation_error is required for the validation repair attempt")
        return self


class CaseDesignOutputV1(FrozenModel):
    validation_status: Literal["pass", "needs_fix"]
    validation_attempt: Literal[0, 1]
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    artifacts: tuple[ArtifactDigestV1, ...] = ()

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[ArtifactDigestV1, ...]) -> tuple[ArtifactDigestV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("case-design artifact paths must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _status_matches_error(self) -> CaseDesignOutputV1:
        if self.validation_status == "pass" and self.validation_error is not None:
            raise ValueError("passing case design cannot carry validation_error")
        if self.validation_status == "needs_fix" and (
            self.validation_attempt != 1 or self.validation_error is None
        ):
            raise ValueError("case design needing repair requires attempt 1 and validation_error")
        return self
