"""Shared automatic-repair item checks for case review and case repair."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    ReviewRepairActionV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)


def auto_fix_actions(
    document: CaseReviewResultV1,
    *,
    error: type[Exception],
    allowed: set[str] | None = None,
    case_delta_paths: tuple[str, ...] = (),
    review: bool = False,
) -> tuple[ReviewRepairActionV1, ...]:
    """Check one auto-fix plan.

    ``review`` adds the case-review-only flag, duplicate, and case-id-field rules.
    The exception type stays with the caller: output for review, input for repair.
    """
    findings = {finding.id: finding for finding in document.findings}
    planned: set[str] = set()
    actions: list[ReviewRepairActionV1] = []
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            raise error(
                "case review auto_fix_plan items must be mappings"
                if review
                else "case-review auto_fix_plan items must be mappings"
            )
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise error(
                "case review auto_fix_plan finding_id must reference an existing finding"
                if review
                else "case-review repair finding_id must reference an existing finding"
            )
        if review and finding_id in planned:
            raise error("case review auto_fix_plan must not duplicate a finding_id")
        planned.add(finding_id)
        finding = findings[finding_id]
        if review:
            flags = finding.model_extra or {}
            if flags.get("auto_fix_allowed") is not True or flags.get("human_review_required") is not False:
                raise error("automatic repair finding must explicitly allow auto-fix and forbid human review")
        if finding.severity in {"critical", "blocking"}:
            raise error(
                "critical or blocking case review finding cannot be auto-fixed"
                if review
                else "critical or blocking case-review findings cannot be auto-fixed"
            )
        artifact = item.get("artifact")
        if review and (not isinstance(artifact, str) or artifact not in (allowed or set())):
            raise error(
                f"automatic repair artifact is outside the locked case-design write set: {artifact!r}"
            )
        if not isinstance(artifact, str) or artifact != finding.locator.artifact:
            raise error(
                "automatic repair artifact must match the finding locator"
                if review
                else "case-review repair artifact must match its finding locator"
            )
        try:
            case_id = normalized_auto_fix_case_id(item, finding.locator.case_id)
            edits = normalized_auto_fix_edits(item)
        except ValueError as raised:
            raise error(str(raised)) from raised
        if case_id != finding.locator.case_id:
            raise error(
                "automatic repair case_id must match the finding locator"
                if review
                else "case-review repair case_id must match its finding locator"
            )
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise error("automatic case repair requires an exact locator key")
        locator_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        if review and artifact in case_delta_paths and "case_id" in locator_paths:
            raise error(
                "case_id identifies the case and cannot be an automatic repair field; "
                "use added or modified to authorize whole-case removal"
            )
        try:
            actions.append(
                ReviewRepairActionV1(
                    finding_id=finding_id,
                    artifact=artifact,
                    case_id=case_id if not review else finding.locator.case_id,
                    allowed_paths=locator_paths,
                    instructions=edits,
                )
            )
        except ValidationError as raised:
            raise error(
                f"invalid automatic repair locator: {raised}"
                if review
                else f"invalid case-review repair action: {raised}"
            ) from raised
    return tuple(actions)
