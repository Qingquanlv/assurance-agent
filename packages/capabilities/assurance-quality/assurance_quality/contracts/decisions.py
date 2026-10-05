from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_quality.contracts.assessment import (
    FailureClassificationFactsV1,
    InspectionDisposition,
)
from assurance_quality.contracts.coverage import COVERAGE_STATES, CoverageState
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import (
    FinalizedIssueAnalysisV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
)
from assurance_quality.contracts.issues import FailureClassification
from graph_engine.plugin_api import FrozenModel
from assurance_quality.contracts.obligations import ObligationGateDecision

FIX_ELIGIBLE_CLASSIFICATIONS: frozenset[str] = frozenset({"test", "test-data"})


class IssueAnalysisPublicV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: FailureClassification
    fix_eligible: bool

    @model_validator(mode="after")
    def _fix_eligible_only_for_test_kinds(self) -> Self:
        if self.fix_eligible and self.classification not in FIX_ELIGIBLE_CLASSIFICATIONS:
            raise ValueError("fix_eligible is only valid for test or test-data classification")
        return self


class CoverageAssessmentPublicV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_state: CoverageState
    rounds_budget: int
    rounds_used: int


IssueRoute = Literal["fix_eligible", "report_issue", "unclassified"]
AnalysisOutcome = Literal["fix_eligible", "report_issue", "unclassified", "failed"]


class IssueTriagePublishedV1(FrozenModel):
    """Triage output the flow routes and exports. ``advice`` keeps the agent result."""

    route: IssueRoute
    classification: FailureClassification
    fix_eligible: bool
    evidence_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    advice: IssueTriageResultV1 | None = None


class IssueAnalysisPublishedV1(FrozenModel):
    """Analysis output. ``issue_analysis`` stays the finalized document retro reads."""

    route: AnalysisOutcome
    classification: FailureClassification
    fix_eligible: bool
    evidence_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    issue_analysis: FinalizedIssueAnalysisV1 | None = None
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None


_REPORT_CLASSIFICATIONS = frozenset({"product_bug", "environment_failure", "infrastructure_failure"})


def triage_route(classification: object, fix_eligible: bool) -> IssueRoute:
    """The old issue-graph table: eligible test debt, known external failures, or neither."""
    if classification in FIX_ELIGIBLE_CLASSIFICATIONS and fix_eligible:
        return "fix_eligible"
    if classification in _REPORT_CLASSIFICATIONS:
        return "report_issue"
    return "unclassified"


def analysis_route(
    result: IssueAnalysisResultV1,
    *,
    inspection_disposition: str | None = None,
) -> AnalysisOutcome:
    """Tail decision when a disposition is supplied; otherwise the graph table.

    The healing budget is a product decision. An exhausted budget still leaves
    this route at ``fix_eligible``; the product sends that entry to needs_human.
    """
    summary = classify_issue_candidates(result)
    if inspection_disposition is None:
        return triage_route(summary.classification, summary.fix_eligible)
    if result.status == "failed":
        return "failed"
    if summary.classification == "unknown":
        return "unclassified"
    if summary.fix_eligible:
        if inspection_disposition != "analysis_required":
            return "report_issue"
        return "fix_eligible"
    return "report_issue"


def classify_issue_candidates(result: IssueAnalysisResultV1) -> IssueAnalysisPublicV1:
    """Only an entirely test-owned batch can authorize a test repair."""
    kinds = {candidate.proposed.classification for candidate in result.candidates}
    classification: FailureClassification = "unknown"
    if result.status == "completed" and kinds and "unknown" not in kinds:
        if kinds <= {"test_bug", "test_data_issue"}:
            classification = "test" if "test_bug" in kinds else "test-data"
        else:
            for kind, target in (
                ("product_bug", "product_bug"),
                ("workflow_issue", "infrastructure_failure"),
                ("environment_issue", "environment_failure"),
                ("performance_issue", "failed"),
                ("coverage_gap", "failed"),
            ):
                if kind in kinds:
                    classification = target  # type: ignore[assignment]
                    break
    return IssueAnalysisPublicV1(
        classification=classification,
        fix_eligible=classification in FIX_ELIGIBLE_CLASSIFICATIONS,
    )


def classify_inspection_disposition(
    *,
    facts: FailureClassificationFactsV1,
    coverage_state: CoverageState | None,
) -> InspectionDisposition:
    if not facts.identity_valid:
        return "blocked"
    if facts.analysis_required:
        return "analysis_required"
    if facts.blocking_failure:
        return "blocked"
    if facts.needs_human:
        return "needs_human"
    if facts.repairable_failure:
        return "repairable_execution_failure"
    if coverage_state == "repair_required":
        return "coverage_insufficient"
    if coverage_state == "satisfied":
        return "satisfied"
    return "blocked"


def merge_obligation_disposition(
    disposition: InspectionDisposition,
    obligation_decision: ObligationGateDecision,
) -> InspectionDisposition:
    """A coverage/test repair cannot override an obligation stop condition."""
    if disposition in {"blocked", "analysis_required"}:
        return disposition
    if obligation_decision == "blocked":
        return "blocked"
    if disposition == "needs_human" or obligation_decision == "needs_human":
        return "needs_human"
    if disposition == "repairable_execution_failure":
        return disposition
    if disposition == "coverage_insufficient" or obligation_decision == "repair_required":
        return "coverage_insufficient"
    return "satisfied"


__all__ = [
    "COVERAGE_STATES",
    "CoverageAssessmentPublicV1",
    "CoverageState",
    "FIX_ELIGIBLE_CLASSIFICATIONS",
    "FailureClassification",
    "IssueAnalysisPublicV1",
    "classify_inspection_disposition",
    "merge_obligation_disposition",
]
