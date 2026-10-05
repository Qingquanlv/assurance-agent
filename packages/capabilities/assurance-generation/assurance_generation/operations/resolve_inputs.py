"""Authenticate the Reviewed Case used by a generation epoch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from graph_engine.artifacts import ArtifactReadError, open_artifact
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from assurance_generation.contracts.workflow import ResolveGenerationInputV1
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.contracts.workflow import require_same_plan


class InputError(ValueError):
    """The requested generation source cannot prove an approved Case version."""


def open_reviewed_case(root: Path, ref: EvidenceArtifactRefV1, error: type[Exception]) -> ReviewedCaseV1:
    """Open the reviewed-case ref. The caller chooses the prepare or finalize error."""

    try:
        return open_artifact(root, ref, model=ReviewedCaseV1)
    except ArtifactReadError as read_error:
        if read_error.reason == "digest":
            raise error(f"reviewed case digest changed: {read_error.path}") from read_error
        raise error(str(read_error)) from read_error


def reviewed_case_field(root: Path, prepared: object, error: type[Exception]) -> dict[str, object]:
    """Load the case when finalize only kept the ref."""

    if not isinstance(prepared, dict):
        return {}
    if prepared.get("reviewed_case") is not None or prepared.get("reviewed_case_ref") is None:
        return {}
    ref = EvidenceArtifactRefV1.model_validate(prepared["reviewed_case_ref"])
    return {"reviewed_case": open_reviewed_case(root, ref, error).model_dump(mode="json")}


def _file(root: Path, ref: EvidenceArtifactRefV1) -> Path:
    path = root
    for part in PurePosixPath(ref.path).parts:
        path = path / part
        if path.is_symlink():
            raise InputError(f"reviewed case input must not contain a symlink: {ref.path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise InputError(f"reviewed case input is missing: {ref.path}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise InputError(f"reviewed case input must be a regular single-link file: {ref.path}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise InputError(f"reviewed case input digest changed: {ref.path}")
    return path


def authenticate_reviewed_case(
    reviewed: ReviewedCaseV1,
    project_root: Path,
    *,
    change_id: str,
    coverage_epoch: int,
) -> ReviewedCaseV1:
    """Recheck every version bound by a Reviewed Case in the current workspace."""
    if reviewed.change_id != change_id:
        raise InputError("reviewed case change_id does not match generation input")
    if reviewed.coverage_epoch != coverage_epoch:
        raise InputError("generation epoch must match the current Reviewed Case")
    try:
        plan = decode_plan(_file(project_root, reviewed.plan_ref).read_bytes(), reviewed.plan_ref)
    except (ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    require_same_plan(
        reviewed.plan_digest,
        reviewed.plan_ref,
        plan.plan_digest,
        reviewed.plan_ref,
    )
    for ref in (*reviewed.preparation_refs, *reviewed.case_refs, reviewed.review_ref, reviewed.selection_ref):
        _file(project_root, ref)
    try:
        raw_review = json.loads(_file(project_root, reviewed.review_ref).read_bytes())
        review = CaseReviewResultV1.model_validate(raw_review)
    except (json.JSONDecodeError, ValidationError) as error:
        raise InputError(f"invalid reviewed case approval: {error}") from error
    if review.change_id != reviewed.change_id or review.public_outcome != "pass":
        raise InputError("generation requires a passing Case Review")
    return reviewed


def resolve_generation_input(data: object, project_root: Path) -> ReviewedCaseV1:
    try:
        request = ResolveGenerationInputV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
    reviewed = None
    if request.reviewed_case_ref is not None:
        reviewed = open_reviewed_case(project_root, request.reviewed_case_ref, InputError)
    standalone = reviewed is None
    if standalone:
        expected = "qa/cases/reviewed-case.json"
        manifests = [item for item in request.source_artifacts if item.path == expected]
        if len(manifests) != 1:
            raise InputError("generation requires exactly one reviewed-case.json artifact")
        path = _file(project_root, manifests[0])
        try:
            reviewed = ReviewedCaseV1.model_validate_json(path.read_bytes())
        except ValidationError as error:
            raise InputError(f"invalid reviewed-case.json: {error}") from error
    authenticated = authenticate_reviewed_case(
        reviewed,
        project_root,
        change_id=request.change_id,
        coverage_epoch=reviewed.coverage_epoch,
    )
    try:
        require_same_plan(
            request.plan_digest,
            request.plan_ref,
            authenticated.plan_digest,
            authenticated.plan_ref,
        )
    except ValueError as error:
        raise InputError(str(error)) from error
    if authenticated.coverage_epoch != request.coverage_epoch:
        # The review's selection is bound to the epoch it was sealed at, so
        # relabelling it here would produce a case that contradicts itself.
        raise InputError("generation epoch must match the current Reviewed Case")
    return authenticated


class ResolveGenerationInputsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            reviewed = resolve_generation_input(request.input, context.project_root)
        except InputError as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=True)
        return TaskOutcome.succeeded(reviewed.model_dump(mode="json"))


__all__ = [
    "InputError",
    "ResolveGenerationInputsHandler",
    "authenticate_reviewed_case",
    "open_reviewed_case",
    "resolve_generation_input",
    "reviewed_case_field",
]
