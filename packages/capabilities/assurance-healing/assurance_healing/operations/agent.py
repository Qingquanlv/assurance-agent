"""Workspace checks shared by healing agent finalize hooks."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from agent_runtime_contracts.ops import InputError, OutputError
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest

from assurance_healing.contracts.agent import FixProposalInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairBrief


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def under_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root.rstrip('/')}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False


def workspace_file(workspace: Path, relative: str) -> Path:
    if not _canonical_relative(relative):
        raise OutputError(f"output file path must be canonical and relative: {relative}")
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if not path.is_file() or path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    if path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _prepare_lock_fields(payload: FixProposalInputV1) -> JSONValue:
    return {
        "baseline_digest": payload.baseline_digest,
        "candidate_digest": payload.candidate_digest,
        "change_id": payload.change_id,
        "plan_digest": payload.plan_digest,
        "plan_ref": payload.plan_ref.model_dump(mode="json"),
        "execution_evidence_digest": payload.execution_evidence_digest,
        "issue_analysis_ref": payload.issue_analysis_ref.model_dump(mode="json")
        if payload.issue_analysis_ref
        else None,
        "policy_digest": payload.policy_digest,
        "require_approval": payload.require_approval,
    }


def require_prepare_lock(current: FixProposalInputV1, locked: FixProposalInputV1) -> None:
    if engine_digest(_prepare_lock_fields(current)) != engine_digest(_prepare_lock_fields(locked)):
        raise InputError("finalize digests do not match the locked prepare payload")


def brief_locator_ids(brief: CoverageRepairBrief) -> set[str]:
    ids: set[str] = set()
    for item in brief.repair_items:
        locator = item.locator
        for value in (locator.case_id, locator.constraint_key, locator.cell, locator.cluster_key):
            if value:
                ids.add(value)
    return ids
