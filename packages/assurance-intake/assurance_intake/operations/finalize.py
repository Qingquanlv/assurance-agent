"""Semantic intake finalize handlers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts import CaseReviewResultV1, CaseYamlAuthoring
from assurance_intake.contracts.agent import AgentFinalizeInputV1, ArtifactListResultV1
from assurance_intake.operations.agent_skills import InputError, failed_input, validate_input


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def _leafs(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _require_known_leafs(keys: Iterable[str], leafs: frozenset[str], *, kind: str) -> None:
    for key in keys:
        if key not in leafs:
            raise OutputError(f"{kind} references unknown capability leaf: {key}")


def _file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _workspace_file(workspace: Path, relative: str) -> Path:
    if not _canonical_relative(relative):
        raise OutputError(f"output file path must be canonical and relative: {relative}")
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _allowed_by_lock(relative: str, locked: tuple[str, ...]) -> bool:
    return any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in locked)


def _authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": _file_digest(path.read_bytes())})
    return artifacts


def _artifact_list(payload: AgentFinalizeInputV1) -> ArtifactListResultV1:
    try:
        return ArtifactListResultV1.model_validate(_structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error


def _finalize_artifact_list(payload: AgentFinalizeInputV1, workspace: Path) -> list[dict[str, str]]:
    if not payload.artifact_paths:
        raise InputError("artifact_paths must lock the expected output files")
    document = _artifact_list(payload)
    return _authenticate_files(workspace, document.output_files, payload.artifact_paths)


class IntakeFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            artifacts = _finalize_artifact_list(payload, context.workspace_root)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ExploreFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            artifacts = _finalize_artifact_list(payload, context.workspace_root)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CaseDesignFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            structured = _structured(payload)
            try:
                document = CaseYamlAuthoring.model_validate(
                    structured,
                    context={"capability_leafs": _leafs(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            return TaskOutcome.succeeded(document.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CaseReviewFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = CaseReviewResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            _require_known_leafs(
                document.minimum_coverage.missing,
                _leafs(payload.capability_leafs),
                kind="case review",
            )
            return TaskOutcome.succeeded(document.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
