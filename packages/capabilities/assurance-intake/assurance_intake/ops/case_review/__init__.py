"""Case review: judge the authored delta against product source and the frozen plan."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, Prepare

from assurance_intake.contracts import CaseYamlAuthoring, MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.cases import QaYaml
from assurance_intake.contracts.review import CaseReviewOutputV1, CaseReviewResultV1
from assurance_intake.contracts.workflow import ReviewedCaseV1
from assurance_intake.handoff import (
    CASE,
    CASE_CATALOG,
    CASE_EXPLORATION,
    CASE_KNOWLEDGE,
    CASE_MARKER,
    CASE_MATRIX,
    CASE_PROPOSAL,
    PLAN,
    PREPARATION,
    REWORK_CONTEXT,
)
from assurance_intake.ops import router
from assurance_intake.ops.case_review import hooks
from assurance_intake.ops.case_review.models import CaseReviewInputV1
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.agent(
    "case-review",
    transport_business=True,
    input=CaseReviewInputV1,
    prepare=Prepare(
        hook=hooks.before,
        depends=(
            PLAN,
            REWORK_CONTEXT,
            CASE,
            PREPARATION,
            CASE_EXPLORATION,
            CASE_CATALOG,
            CASE_KNOWLEDGE,
            CASE_MARKER,
            CASE_PROPOSAL,
            CASE_MATRIX,
        ),
        eager_artifacts=True,
        reads=(
            Out("marker", "qa/.qa.yaml", model=QaYaml, format="yaml"),
            Out("proposal", "qa/proposal.md"),
            Out("requirement", "qa/requirement.md"),
            Out("matrix", hooks.MATRIX_PATH, model=MinimumCoverageMatrixAuthoring, format="json"),
            Dir(
                "qa/cases",
                files=lambda business: business.case_delta_paths,
                model=CaseYamlAuthoring,
                format="yaml",
                context=lambda business: {
                    "capability_leafs": frozenset(getattr(business, "capability_leafs"))
                },
            ),
        ),
    ),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-case-reviewer",
        result=CaseReviewResultV1,
        writes=(
            Out("review", hooks.REVIEW_PATH, model=CaseReviewResultV1, format="json"),
            Out("summary", hooks.SUMMARY_PATH),
        ),
        strict_files=True,
    ),
    finalize=Finalize(
        hook=hooks.after,
        same=("change_id",),
        errors=(ValueError,),
        error_failure="output",
        artifacts="auto",
        writes=(
            Out(
                "reviewed_case",
                hooks.REVIEWED_CASE_PATH,
                model=ReviewedCaseV1,
                format="json",
            ),
            Dir(hooks.REVIEW_HISTORY_ROOT, name="history", accumulate=True),
            hooks.SELECTION_ROOT,
        ),
    ),
    output=CaseReviewOutputV1,
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)

__all__ = ["CaseReviewInputV1", "op"]
