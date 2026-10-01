"""Case review: judge the authored delta against product source and the frozen plan."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_review import hooks
from assurance_intake.ops.case_review.models import CaseReviewInputV1

op = router.agent(
    "case-review",
    input=CaseReviewInputV1,
    prepare=Prepare(hook=hooks.before, depends=(frozen_plan,)),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-case-reviewer",
        result=CaseReviewResultV1,
        writes=(hooks.REVIEW_PATH, hooks.SUMMARY_PATH),
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(hooks.REVIEWED_CASE_PATH, hooks.REVIEW_HISTORY_ROOT, hooks.SELECTION_ROOT),
    ),
    output=CaseReviewResultV1,
)

__all__ = ["CaseReviewInputV1", "op"]
