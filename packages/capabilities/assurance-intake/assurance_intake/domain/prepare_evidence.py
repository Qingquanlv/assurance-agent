"""Authenticate committed Intake evidence before Agent preparation."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, PrepareContext

from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def require_regular_project_input(project_root: Path, relative: str) -> None:
    root = project_root.resolve()
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise InputError(f"case-review input must not contain a symlink: {relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise InputError(f"missing case-review input: {relative}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise InputError(f"case-review input must be a regular single-link file: {relative}")


def authenticate_evidence_refs(
    project_root: Path,
    refs: tuple[EvidenceArtifactRefV1, ...],
) -> None:
    for ref in refs:
        require_regular_project_input(project_root, ref.path)
        path = project_root.joinpath(*ref.path.split("/"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
            raise InputError(f"evidence digest changed after it was committed: {ref.path}")


def authenticate_plan(
    project_root: Path,
    *,
    change_id: str,
    plan_digest: str,
    plan_ref: EvidenceArtifactRefV1,
) -> ResolvedAssurancePlan:
    authenticate_evidence_refs(project_root, (plan_ref,))
    try:
        plan = decode_plan(
            project_root.joinpath(*plan_ref.path.split("/")).read_bytes(),
            plan_ref,
        )
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.change_id != change_id or plan.plan_digest != plan_digest:
        raise InputError("frozen assurance plan does not match case input")
    return plan


class PlanBoundInput(Protocol):
    @property
    def change_id(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    @property
    def plan_ref(self) -> EvidenceArtifactRefV1: ...


def frozen_plan(ctx: PrepareContext, business: PlanBoundInput) -> ResolvedAssurancePlan:
    """Op dependency: the committed plan the business input is bound to."""
    return authenticate_plan(
        ctx.project_root,
        change_id=business.change_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
    )
