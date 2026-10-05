"""Case-review input authentication and automatic-repair scope checks."""

from __future__ import annotations

from agent_runtime_contracts.ops import InputError, OutputError

from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.domain.auto_fix import auto_fix_actions
from assurance_intake.ops.case_review.models import CaseReviewInputV1


def validate_case_review_repair_scope(
    document: CaseReviewResultV1,
    payload: CaseReviewInputV1,
    *,
    change_id: str,
) -> None:
    del change_id
    if document.public_outcome != "needs_fix":
        return
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required to lock case-review automatic repairs")
    change_root = "qa"
    allowed = {
        f"{change_root}/.qa.yaml",
        f"{change_root}/proposal.md",
        f"{change_root}/results/trace/minimum-coverage-matrix.json",
        *payload.case_delta_paths,
    }
    if document.next_action != "run_case_design":
        raise OutputError("needs_fix case review next_action must be run_case_design")
    if not document.auto_fix_plan:
        raise OutputError("needs_fix case review must provide at least one auto_fix_plan item")
    auto_fix_actions(
        document,
        error=OutputError,
        allowed=allowed,
        case_delta_paths=payload.case_delta_paths,
        review=True,
    )
