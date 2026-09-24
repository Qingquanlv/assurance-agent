"""Provider-neutral fix-proposal and coverage-repair prepare/finalize handlers."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    InstructionPart,
    ResultContract,
    prompt_model_json,
    with_validation_retry,
)
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import (
    AgentBindingDataV1,
    FixProposalFinalizeInputV1,
    CoverageRepairFinalizeInputV1,
    CoverageRepairInputV1,
    FixProposalInputV1,
    FixProposalResultV1,
)
from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary, CoverageRepairBrief
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
_BOUNDED_PROFILES = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
}


def agent_workspace(
    context: TaskContext,
    *,
    allowed_outputs: tuple[str, ...],
    agent_profile: str,
    scope_id: str,
) -> AgentWorkspaceV1:
    try:
        write_root = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        write_root = "qa/.staging/write"
    if write_root in {".", ""}:
        write_root = ".staging/write"
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
        "read_roots": (),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def result_contract(schema_id: str, relative: str) -> ResultContract:
    payload = json.loads(resource_bytes(relative))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        delivery_mode="assistant_json_local_v1",
        schema_document=payload,
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_id: str,
    result_file: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=with_validation_retry(
            (
                InstructionPart.text("text/plain", resource_text(skill_path)),
                InstructionPart.text("text/plain", resource_text(FIX_PROPOSER_PERSONA)),
                InstructionPart.from_json(prompt_model_json(business)),
            ),
            getattr(business, "validation_error", None),
        ),
        result_contract=result_contract(result_id, result_file),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
            scope_id=business.change_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
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
    if path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _structured(result: object) -> object:
    return thaw_json(result)


def _prepare_lock_fields(payload: FixProposalFinalizeInputV1 | FixProposalInputV1) -> JSONValue:
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


def _require_prepare_lock(payload: FixProposalFinalizeInputV1) -> None:
    if engine_digest(_prepare_lock_fields(payload)) != engine_digest(_prepare_lock_fields(payload.prepare)):
        raise InputError("finalize digests do not match the locked prepare payload")


def _brief_locator_ids(brief: CoverageRepairBrief) -> set[str]:
    ids: set[str] = set()
    for item in brief.repair_items:
        locator = item.locator
        for value in (locator.case_id, locator.constraint_key, locator.cell, locator.cluster_key):
            if value:
                ids.add(value)
    return ids


class FixProposalPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(FixProposalInputV1, request.input)
            if business.issue_analysis_ref is not None:
                ref = business.issue_analysis_ref
                data = _workspace_file(context.project_root, ref.path).read_bytes()
                if hashlib.sha256(data).hexdigest() != ref.digest:
                    raise OutputError("issue analysis digest changed")
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=FIX_PROPOSAL_SKILL,
                business=business,
                binding=binding,
                result_id=FIX_PROPOSAL_RESULT_ID,
                result_file=_FIX_RESULT_FILE,
                context=context,
                allowed_outputs=("qa/results/healing/fix-proposal.json",),
            )
        except (InputError, OutputError) as error:
            return failed_input(error)


class FixProposalFinalizeHandler:
    input_model = FixProposalFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(FixProposalFinalizeInputV1, request.input)
            _require_prepare_lock(business)
            agent_result = business.agent_result
            unknown = [
                item for item in business.claimed_capabilities if item not in business.capability_leafs
            ]
            if unknown:
                raise OutputError(f"unknown capability: {unknown[0]}")
            try:
                proposal = FixProposalResultV1.model_validate(_structured(agent_result.result_payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if proposal.change_id != business.change_id:
                raise OutputError("proposal change_id does not match the locked change")
            allowed = set(business.allowed_paths)
            for item in proposal.proposals:
                if not item.eligible:
                    continue
                if not item.files_to_modify:
                    raise OutputError("eligible proposal requires files_to_modify")
                for path in item.files_to_modify:
                    if path not in allowed or not _under_root(path, business.allowed_roots):
                        raise OutputError(f"undeclared target file: {path}")
                    _workspace_file(context.project_root, path)
            relative = "qa/results/healing/fix-proposal.json"
            staged = _workspace_file(context.write_root, relative)
            try:
                staged_proposal = FixProposalResultV1.model_validate(
                    json.loads(staged.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, ValidationError) as error:
                raise OutputError("staged fix proposal is not the typed agent result") from error
            if staged_proposal != proposal:
                raise OutputError("staged fix proposal differs from the typed agent result")
            expected = canonical_json_bytes(cast(JSONValue, proposal.model_dump(mode="json"))) + b"\n"
            if staged.read_bytes() != expected:
                raise OutputError("staged fix proposal must use canonical JSON bytes")
            return TaskOutcome.succeeded(cast(JSONValue, proposal.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CoverageRepairPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(CoverageRepairInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=COVERAGE_REPAIR_SKILL,
                business=business,
                binding=binding,
                result_id=COVERAGE_REPAIR_RESULT_ID,
                result_file=_REPAIR_RESULT_FILE,
                context=context,
                allowed_outputs=("qa/results/healing/coverage-repair.json",),
            )
        except InputError as error:
            return failed_input(error)


class CoverageRepairFinalizeHandler:
    input_model = CoverageRepairFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(CoverageRepairFinalizeInputV1, request.input)
            if (
                payload.baseline_digest != payload.prepare.baseline_digest
                or payload.change_id != payload.prepare.change_id
                or payload.brief != payload.prepare.brief
            ):
                raise InputError("finalize digests do not match the locked prepare payload")
            try:
                summary = CoverageRepairApplySummary.model_validate(
                    _structured(payload.agent_result.result_payload)
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if summary.change_id != payload.change_id or summary.change_id != payload.brief.change_id:
                raise OutputError("coverage repair identity does not match the locked change")
            locators = _brief_locator_ids(payload.brief)
            unknown = [item for item in summary.addressed_items if item not in locators]
            if unknown:
                raise OutputError(f"addressed item is not a brief locator: {unknown[0]}")
            allowed = set(payload.brief.allowed_test_files)
            for path in summary.files_modified:
                if path not in allowed or not _under_root(path, payload.allowed_roots):
                    raise OutputError(f"undeclared target file: {path}")
                _workspace_file(context.project_root, path)
            for path in payload.artifact_paths:
                _workspace_file(context.project_root, path)
            return TaskOutcome.succeeded(cast(JSONValue, summary.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
