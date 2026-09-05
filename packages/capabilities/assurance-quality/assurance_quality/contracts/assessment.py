"""Closed contracts at the deterministic Inspect boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_quality.contracts.coverage import CoverageState

InspectionDisposition = Literal[
    "satisfied",
    "coverage_insufficient",
    "repairable_execution_failure",
    "needs_human",
    "blocked",
]


class InspectionOutcomeV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    disposition: InspectionDisposition
    inspection_receipt: ReceiptRef
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    assessment_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...]
    coverage_state: CoverageState | None = None

    @model_validator(mode="after")
    def _identity_and_disposition_are_closed(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("inspection Reviewed Case change_id must match")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("inspection Reviewed Case epoch must match")
        if self.disposition == "satisfied" and self.coverage_state != "satisfied":
            raise ValueError("satisfied inspection requires satisfied coverage")
        if self.disposition == "coverage_insufficient" and self.coverage_state not in {
            "repair_required",
            "exhausted",
        }:
            raise ValueError("coverage insufficiency requires an insufficient coverage state")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("inspection reason_codes must be sorted and unique")
        return self


__all__ = ["InspectionDisposition", "InspectionOutcomeV1"]
