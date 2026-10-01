"""Immutable current-change case selection, including reuse."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import SHA256_PATTERN
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

ALLOWED_SELECTION_ROOTS = ("qa/cases/",)


class SelectedCaseV1(FrozenModel):
    case_id: str = Field(min_length=1)
    origin: Literal["added", "modified", "reuse"]
    source_ref: EvidenceArtifactRefV1
    source_locator: str = Field(min_length=1)
    mrc_ids: tuple[str, ...]

    @field_validator("source_ref")
    @classmethod
    def _historical_source_is_bounded(cls, value: EvidenceArtifactRefV1) -> EvidenceArtifactRefV1:
        if not value.path.startswith(ALLOWED_SELECTION_ROOTS):
            raise ValueError("selection source must stay under qa/cases/")
        if not value.path.endswith("/case.yaml"):
            raise ValueError("selection source must be a case.yaml")
        return value


class CaseSelectionV1(FrozenModel):
    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    inventory_ref: EvidenceArtifactRefV1
    cases: tuple[SelectedCaseV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_case_ids(self) -> CaseSelectionV1:
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("selection case_id must be unique")
        return self


def selection_path(coverage_epoch: int) -> str:
    return f"qa/results/cases/epochs/{coverage_epoch}/selection.json"
