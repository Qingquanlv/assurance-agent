"""Authenticate the Reviewed Case used by a generation epoch."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from assurance_generation.contracts.workflow import ResolveGenerationInputV1
from assurance_intake.contracts.reviewed_case import (
    ReviewedCaseAuthenticationError as InputError,
    authenticate_reviewed_case,
    authenticated_file as _file,
)
from assurance_intake.contracts.workflow import ReviewedCaseV1
from assurance_intake.contracts.workflow import require_same_plan


def resolve_generation_input(data: object, project_root: Path) -> ReviewedCaseV1:
    try:
        request = ResolveGenerationInputV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
    reviewed = request.reviewed_case
    standalone = reviewed is None
    if standalone:
        expected = f"qa/changes/{request.change_id}/cases/reviewed-case.json"
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
    if not standalone and authenticated.coverage_epoch != request.coverage_epoch:
        raise InputError("full generation epoch must match the current Reviewed Case")
    if standalone and authenticated.coverage_epoch != request.coverage_epoch:
        return authenticated.model_copy(update={"coverage_epoch": request.coverage_epoch})
    return authenticated


class ResolveGenerationInputsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            reviewed = resolve_generation_input(request.input, context.project_root)
        except InputError as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(reviewed.model_dump(mode="json"))


__all__ = [
    "InputError",
    "ResolveGenerationInputsHandler",
    "authenticate_reviewed_case",
    "resolve_generation_input",
]
