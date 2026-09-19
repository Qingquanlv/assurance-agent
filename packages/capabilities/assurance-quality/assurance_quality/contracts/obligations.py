from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.obligations import SourceRefV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

ObligationVerdict = Literal["supported", "refuted", "inconclusive"]


class ObligationEvidenceFactsV1(FrozenModel):
    """Derived per-obligation facts. Produced only from authenticated evidence."""

    expectation_confirmed: bool
    eligible_counterexample: bool
    subject_valid: bool
    method_valid: bool
    prerequisites_valid: bool
    observations_complete: bool
    required_reviews_passed: bool
    supporting_evidence_current: bool
    counterexample_evidence_current: bool
    counterexample_refs: tuple[EvidenceArtifactRefV1, ...] = ()

    @model_validator(mode="after")
    def _eligible_counterexample_is_qualified(self) -> Self:
        if not self.eligible_counterexample:
            return self
        required = (
            self.expectation_confirmed,
            self.subject_valid,
            self.method_valid,
            self.prerequisites_valid,
            self.required_reviews_passed,
            self.counterexample_evidence_current,
        )
        if not all(required) or not self.counterexample_refs:
            raise ValueError("eligible counterexample requires confirmed expected, subject, method, and refs")
        return self


class ObligationAssessmentRowV1(FrozenModel):
    plan_digest: str
    mrc_id: str
    verdict: ObligationVerdict
    method_plan_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    evidence_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    gap_codes: tuple[str, ...] = ()
    remaining_assumptions: tuple[str, ...] = ()


class ObligationAssessmentV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    plan_ref: EvidenceArtifactRefV1
    rows: tuple[ObligationAssessmentRowV1, ...]
    excluded_mrc_ids: tuple[str, ...] = ()
    excluded_basis_refs: tuple[SourceRefV1, ...] = ()


class ObligationGateFactsV1(FrozenModel):
    required_count: int = Field(ge=0)
    supported_count: int = Field(ge=0)
    refuted_count: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    repairable_gap_count: int = Field(ge=0)
    human_gap_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts_are_closed(self) -> Self:
        if self.required_count != self.supported_count + self.refuted_count + self.inconclusive_count:
            raise ValueError("required_count must equal supported + refuted + inconclusive")
        if self.human_gap_count + self.repairable_gap_count > self.inconclusive_count:
            raise ValueError("classified gaps cannot exceed inconclusive obligations")
        return self


ObligationGateDecision = Literal["satisfied", "repair_required", "needs_human", "blocked"]


__all__ = [
    "ObligationAssessmentRowV1",
    "ObligationAssessmentV1",
    "ObligationEvidenceFactsV1",
    "ObligationGateDecision",
    "ObligationGateFactsV1",
    "ObligationVerdict",
]
