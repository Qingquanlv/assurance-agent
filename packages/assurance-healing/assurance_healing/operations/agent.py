"""Provider-neutral fix-proposal and coverage-repair prepare/finalize handlers."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    CoverageRepairFinalizeInputV1,
    CoverageRepairInputV1,
    FixProposalInputV1,
    FixProposalResultV1,
)
from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary
from assurance_healing.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_input,
)
from assurance_healing.resource_loader import resource_bytes, resource_text

FIX_PROPOSAL_SKILL = "skills/aa-fix-proposal/SKILL.md"
COVERAGE_REPAIR_SKILL = "skills/aa-coverage-repair/SKILL.md"
FIX_PROPOSER_PERSONA = "personas/fix-proposer.md"
FIX_PROPOSAL_RESULT_ID = "assurance.healing.result.fix-proposal.v1"
COVERAGE_REPAIR_RESULT_ID = "assurance.healing.result.coverage-repair.v1"
_FIX_RESULT_FILE = "result-contracts/fix-proposal.v1.schema.json"
_REPAIR_RESULT_FILE = "result-contracts/coverage-repair.v1.schema.json"


def result_contract(schema_id: str, relative: str) -> ResultContract:
    payload = json.loads(resource_bytes(relative))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *, skill_path: str, business: Any, binding: AgentBindingDataV1, result_id: str, result_file: str
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(FIX_PROPOSER_PERSONA)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(result_id, result_file),
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


def _under_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root.rstrip('/')}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False


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
    if not path.is_file() or path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    return path


def _structured(result: object) -> object:
    return thaw_json(result)


class FixProposalPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business = validate_input(FixProposalInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=FIX_PROPOSAL_SKILL,
                business=business,
                binding=binding,
                result_id=FIX_PROPOSAL_RESULT_ID,
                result_file=_FIX_RESULT_FILE,
            )
        except InputError as error:
            return failed_input(error)


class FixProposalFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            unknown = [item for item in payload.claimed_capabilities if item not in payload.capability_leafs]
            if unknown:
                raise OutputError(f"unknown capability: {unknown[0]}")
            try:
                proposal = FixProposalResultV1.model_validate(
                    _structured(payload.agent_result.structured_result)
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if proposal.change_id != payload.change_id:
                raise OutputError("proposal change_id does not match the locked change")
            allowed = set(payload.allowed_paths) | set(payload.mapping_paths)
            for item in proposal.proposals:
                if not item.eligible:
                    continue
                if not item.files_to_modify:
                    raise OutputError("eligible proposal requires files_to_modify")
                for path in item.files_to_modify:
                    if path not in allowed or not _under_root(path, payload.allowed_roots):
                        raise OutputError(f"undeclared target file: {path}")
                    _workspace_file(context.workspace_root, path)
            if payload.artifact_paths:
                for path in payload.artifact_paths:
                    _workspace_file(context.workspace_root, path)
            return TaskOutcome.succeeded(cast(JSONValue, proposal.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CoverageRepairPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business = validate_input(CoverageRepairInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=COVERAGE_REPAIR_SKILL,
                business=business,
                binding=binding,
                result_id=COVERAGE_REPAIR_RESULT_ID,
                result_file=_REPAIR_RESULT_FILE,
            )
        except InputError as error:
            return failed_input(error)


class CoverageRepairFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(CoverageRepairFinalizeInputV1, request.input)
            try:
                summary = CoverageRepairApplySummary.model_validate(
                    _structured(payload.agent_result.structured_result)
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if summary.change_id != payload.change_id or summary.change_id != payload.brief.change_id:
                raise OutputError("coverage repair identity does not match the locked change")
            allowed = set(payload.brief.allowed_test_files)
            for path in summary.files_modified:
                if path not in allowed or not _under_root(path, payload.allowed_roots):
                    raise OutputError(f"undeclared target file: {path}")
                _workspace_file(context.workspace_root, path)
            for path in payload.artifact_paths:
                _workspace_file(context.workspace_root, path)
            return TaskOutcome.succeeded(cast(JSONValue, summary.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
