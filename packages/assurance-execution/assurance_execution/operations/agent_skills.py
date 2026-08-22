"""Provider-neutral execute/run prepare and finalize handlers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    ExecuteInputV1,
    RunSkillInputV1,
)
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    leafs_of,
    validate_input,
)
from assurance_execution.resource_loader import resource_bytes, resource_text

EXECUTE_SKILL = "skills/aa-execute/SKILL.md"
RUN_SKILL = "skills/aa-run/SKILL.md"
EXECUTOR_PERSONA = "personas/executor.md"
EXECUTION_RESULT_ID = "assurance.execution.result.execution.v1"
_RESULT_FILE = "result-contracts/execution.v1.schema.json"


def result_contract() -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILE))
    return ResultContract(
        schema_id=EXECUTION_RESULT_ID,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(*, skill_path: str, business: Any, binding: AgentBindingDataV1) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(EXECUTOR_PERSONA)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(),
        execution=binding.execution,
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
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
    return path


def _authenticate_files(workspace: Path, declared: tuple[str, ...]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": hashlib.sha256(path.read_bytes()).hexdigest()})
    return artifacts


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _finalize_evidence(payload: AgentFinalizeInputV1, workspace: Path) -> ExecutionEvidenceV1:
    leafs = leafs_of(payload.capability_leafs)
    case_ids = leafs_of(payload.case_ids)
    try:
        locked = ClosedMappingV1.model_validate(
            payload.mapping.model_dump(mode="json"),
            context={"capability_leafs": leafs, "case_ids": case_ids},
        )
        evidence = ExecutionEvidenceV1.model_validate(
            _structured(payload),
            context={"capability_leafs": leafs, "case_ids": case_ids},
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if evidence.mapping != locked:
        raise OutputError("execution evidence mapping does not match the locked closed mapping")
    if evidence.change_id != payload.change_id or evidence.batch_id != payload.batch_id:
        raise OutputError("execution evidence identity does not match the locked change")
    if evidence.baseline_tree_id != payload.baseline_tree_id:
        raise OutputError("execution evidence baseline tree does not match the authenticated workspace")
    if payload.artifact_paths:
        _authenticate_files(workspace, payload.artifact_paths)
    return evidence


class ExecutePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business = validate_input(ExecuteInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(skill_path=EXECUTE_SKILL, business=business, binding=binding)
        except InputError as error:
            return failed_input(error)


class RunPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business = validate_input(RunSkillInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(skill_path=RUN_SKILL, business=business, binding=binding)
        except InputError as error:
            return failed_input(error)


class ExecuteFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            evidence = _finalize_evidence(payload, context.workspace_root)
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RunFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            evidence = _finalize_evidence(payload, context.workspace_root)
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
