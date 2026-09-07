"""Authenticate one committed ReviewedCase without depending on intake operations."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest

from assurance_intake.contracts.loop_history import LoopRoundHistoryV1
from assurance_intake.contracts.plan import decode_plan
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan


class ReviewedCaseAuthenticationError(ValueError):
    """The committed files do not prove the claimed ReviewedCase approval."""


def _regular_file(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ReviewedCaseAuthenticationError(
                f"reviewed case input must not contain a symlink: {relative}"
            )
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise ReviewedCaseAuthenticationError(f"reviewed case input is missing: {relative}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise ReviewedCaseAuthenticationError(
            f"reviewed case input must be a regular single-link file: {relative}"
        )
    return path


def authenticated_file(root: Path, ref: EvidenceArtifactRefV1) -> Path:
    """Resolve one digest-bound regular file beneath a project root."""

    path = _regular_file(root, ref.path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise ReviewedCaseAuthenticationError(f"reviewed case input digest changed: {ref.path}")
    return path


def authenticate_reviewed_case(
    reviewed: ReviewedCaseV1,
    project_root: Path,
    *,
    change_id: str,
    coverage_epoch: int,
    require_acceptance_record: bool = False,
) -> ReviewedCaseV1:
    """Recheck the plan, input refs, and passing typed review for a ReviewedCase."""

    if reviewed.change_id != change_id:
        raise ReviewedCaseAuthenticationError("reviewed case change_id does not match generation input")
    if reviewed.coverage_epoch != coverage_epoch:
        raise ReviewedCaseAuthenticationError("generation epoch must match the current Reviewed Case")
    try:
        plan = decode_plan(
            authenticated_file(project_root, reviewed.plan_ref).read_bytes(), reviewed.plan_ref
        )
    except (ValidationError, ValueError) as error:
        raise ReviewedCaseAuthenticationError(f"invalid frozen assurance plan: {error}") from error
    require_same_plan(
        reviewed.plan_digest,
        reviewed.plan_ref,
        plan.plan_digest,
        reviewed.plan_ref,
    )
    for ref in (*reviewed.preparation_refs, *reviewed.case_refs, reviewed.review_ref):
        authenticated_file(project_root, ref)
    try:
        review = CaseReviewResultV1.model_validate_json(
            authenticated_file(project_root, reviewed.review_ref).read_bytes()
        )
    except ValidationError as error:
        raise ReviewedCaseAuthenticationError(f"invalid reviewed case approval: {error}") from error
    if review.change_id != reviewed.change_id or review.public_outcome != "pass":
        raise ReviewedCaseAuthenticationError("generation requires a passing Case Review")
    if require_acceptance_record:
        manifest_path = _regular_file(project_root, f"qa/changes/{change_id}/cases/reviewed-case.json")
        try:
            accepted = ReviewedCaseV1.model_validate_json(manifest_path.read_bytes())
        except ValidationError as error:
            raise ReviewedCaseAuthenticationError(
                f"invalid ReviewedCase acceptance record: {error}"
            ) from error
        if accepted != reviewed:
            raise ReviewedCaseAuthenticationError("ReviewedCase differs from the committed acceptance record")
        input_refs = tuple(
            sorted((*reviewed.preparation_refs, *reviewed.case_refs), key=lambda item: item.path)
        )
        input_digest = canonical_digest(
            cast(JSONValue, [item.model_dump(mode="json") for item in input_refs])
        )
        expected_sources = tuple(sorted((*input_refs, reviewed.review_ref), key=lambda item: item.path))
        history_root = (
            project_root
            / "qa"
            / "changes"
            / change_id
            / "cases"
            / "reviews"
            / "epochs"
            / str(coverage_epoch)
            / "rounds"
        )
        matches: list[LoopRoundHistoryV1] = []
        for path in sorted(history_root.glob("*.json")):
            relative = path.relative_to(project_root).as_posix()
            try:
                history = LoopRoundHistoryV1.model_validate_json(
                    _regular_file(project_root, relative).read_bytes()
                )
            except ValidationError as error:
                raise ReviewedCaseAuthenticationError(
                    f"invalid Case Review acceptance history: {error}"
                ) from error
            if (
                history.change_id == change_id
                and history.coverage_epoch == coverage_epoch
                and history.loop_kind == "case_review"
                and history.family is None
                and history.outcome == "pass"
                and history.review_input_digest == input_digest
                and history.source_refs == expected_sources
            ):
                matches.append(history)
        if len(matches) != 1:
            raise ReviewedCaseAuthenticationError(
                "ReviewedCase requires one exact passing Case Review acceptance history"
            )
    return reviewed


__all__ = [
    "ReviewedCaseAuthenticationError",
    "authenticate_reviewed_case",
    "authenticated_file",
]
