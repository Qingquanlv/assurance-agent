"""Case review: judge the authored delta against product source and the frozen plan."""

from __future__ import annotations

from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_review import hooks
from assurance_intake.ops.case_review.models import CaseReviewInputV1

op = router.agent(
    "case-review",
    profile="assurance-v1-reviewer",
    skill="aa-case-reviewer",
    input=CaseReviewInputV1,
    result=CaseReviewResultV1,
    output=CaseReviewResultV1,
    writes=(hooks.REVIEW_PATH, hooks.SUMMARY_PATH),
    finalize_writes=(hooks.REVIEWED_CASE_PATH, hooks.REVIEW_HISTORY_ROOT, hooks.SELECTION_ROOT),
    routes=(
        hooks.REVIEW_PATH,
        hooks.SUMMARY_PATH,
        hooks.REVIEWED_CASE_PATH,
        f"{hooks.REVIEW_HISTORY_ROOT}/epochs/{{coverage_epoch}}/rounds/{{review_round}}.json",
        f"{hooks.SELECTION_ROOT}/{{coverage_epoch}}/selection.json",
    ),
    depends=(frozen_plan,),
    before=hooks.before,
    after=hooks.after,
)

__all__ = ["CaseReviewInputV1", "op"]
