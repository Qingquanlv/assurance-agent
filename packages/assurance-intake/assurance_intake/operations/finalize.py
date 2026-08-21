"""Semantic intake finalize handlers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts import CaseReviewResultV1, CaseYamlAuthoring
from assurance_intake.contracts.agent import AgentFinalizeInputV1
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


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OutputError("structured result must be an object")
    return value


def _output_files(structured: Mapping[str, Any]) -> tuple[str, ...]:
    raw = structured.get("output_files")
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise OutputError("structured result output_files must be a list of paths")
    return tuple(str(item) for item in raw)


def _file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    expected = set(locked) if locked else set(declared)
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if expected and relative not in expected:
            raise OutputError(f"undeclared output file: {relative}")
        path = workspace.joinpath(*Path(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": _file_digest(path.read_bytes())})
    return artifacts


def _review_capability_keys(structured: Mapping[str, Any]) -> tuple[str, ...]:
    raw = structured.get("required_capabilities")
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise OutputError("required_capabilities must be a list of strings")
    return tuple(str(item) for item in raw)


class IntakeFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            structured = _mapping(_structured(payload))
            artifacts = _authenticate_files(
                context.workspace_root,
                _output_files(structured),
                payload.artifact_paths,
            )
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ExploreFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            structured = _mapping(_structured(payload))
            artifacts = _authenticate_files(
                context.workspace_root,
                _output_files(structured),
                payload.artifact_paths,
            )
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
            structured = _mapping(_structured(payload))
            _require_known_leafs(
                _review_capability_keys(structured),
                _leafs(payload.capability_leafs),
                kind="case review",
            )
            try:
                document = CaseReviewResultV1.model_validate(structured)
            except ValidationError as error:
                raise OutputError(str(error)) from error
            return TaskOutcome.succeeded(document.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
