from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

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


__all__ = [
    "ObligationAssessmentRowV1",
    "ObligationAssessmentV1",
    "ObligationEvidenceFactsV1",
    "ObligationVerdict",
]
