"""Provider-neutral execute/run prepare and finalize handlers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError
import yaml

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    ExecutionFinalizeRequestV1,
    ExecutionPrepareInputV1,
    ExecuteInputV1,
    RunSkillInputV1,
    SelectInputV1,
)
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_execution.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    json_digest,
    leafs_of,
    mapping_digest,
    validate_input,
)
from assurance_execution.operations.runner import write_canonical_evidence
from assurance_execution.operations.selection import close_mappings
from assurance_generation.contracts import CodegenAuthoringV1
from assurance_intake.contracts import CaseYamlAuthoring
from assurance_execution.resource_loader import resource_bytes, resource_text

EXECUTE_SKILL = "skills/aa-execute/SKILL.md"
RUN_SKILL = "skills/aa-run/SKILL.md"
EXECUTOR_PERSONA = "personas/executor.md"
EXECUTION_RESULT_ID = "assurance.execution.result.execution.v1"
_RESULT_FILE = "result-contracts/execution.v1.schema.json"
_RUNNER_PROFILE_DIGEST = canonical_digest(
    {
        "profile": "assurance.execution.agent.v1",
        "selection": "closed-mapping-test-selector.v1",
        "evidence": "assurance.execution.result.execution.v1",
    }
)
_BOUNDED_PROFILES = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
}


def result_contract() -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILE))
    return ResultContract(
        schema_id=EXECUTION_RESULT_ID,
        schema_digest=canonical_digest(payload),
        delivery_mode="assistant_json_local_v1",
        schema_document=payload,
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def _agent_workspace(
    context: TaskContext,
    *,
    agent_profile: str,
    allowed_outputs: tuple[str, ...],
    scope_id: str,
) -> AgentWorkspaceV1:
    try:
        write_root = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        write_root = "qa/changes/_attempt/.staging/write"
    if write_root in {".", ""}:
        write_root = ".staging/write"
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def _execution_outputs(change_id: str, *, run: bool) -> tuple[str, ...]:
    name = "run-result.json" if run else "execute-result.json"
    return (f"qa/changes/{change_id}/execution/{name}",)


def prepare_outcome(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(EXECUTOR_PERSONA)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(),
        execution=binding.execution,
        workspace=_agent_workspace(
            context,
            agent_profile=binding.agent_profile,
            allowed_outputs=allowed_outputs,
            scope_id=business.change_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _regular_input_file(workspace: Path, relative: str) -> Path:
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError(f"execution input escapes the attempt workspace: {relative}") from error
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise InputError(f"execution input is not a regular single-link file: {relative}")
    return path


def _json_document(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputError(f"execution input is not valid JSON: {path}") from error


def _reviewed_cases(
    workspace: Path,
    *,
    change_id: str,
    capability_leafs: tuple[str, ...],
) -> CaseYamlAuthoring:
    root = workspace / "qa" / "changes" / change_id / "cases"
    if not root.is_dir() or root.is_symlink():
        raise InputError(f"reviewed case directory is missing: qa/changes/{change_id}/cases")
    paths = tuple(sorted(root.glob("**/case.yaml"), key=lambda item: item.as_posix()))
    if not paths:
        raise InputError(f"reviewed case files are missing: qa/changes/{change_id}/cases/**/case.yaml")
    added: list[object] = []
    modified: list[object] = []
    versions: set[str] = set()
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        try:
            document = yaml.safe_load(_regular_input_file(workspace, relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise InputError(f"execution case input is not valid YAML: {relative}") from error
        if not isinstance(document, dict):
            raise InputError(f"execution case input must be an object: {relative}")
        version = document.get("schema_version")
        if isinstance(version, str) and version.strip():
            versions.add(version)
        for label, destination in (("added", added), ("modified", modified)):
            entries = document.get(label)
            if not isinstance(entries, list):
                raise InputError(f"{relative}: {label} must be a list")
            destination.extend(entries)
    if len(versions) != 1:
        raise InputError("reviewed case files must use one non-empty schema_version")
    try:
        return CaseYamlAuthoring.model_validate(
            {
                "schema_version": next(iter(versions)),
                "added": added,
                "modified": modified,
                "removed": [],
            },
            context={"capability_leafs": leafs_of(capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error


def _workspace_tree_id(workspace: Path) -> str:
    manifest: dict[str, str] = {}
    for root, directories, filenames in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *filenames):
            if (root_path / name).is_symlink():
                raise InputError("attempt workspace contains a symbolic link")
        for name in filenames:
            path = root_path / name
            if not path.is_file() or path.stat().st_nlink != 1:
                raise InputError("attempt workspace contains a non-regular file")
            relative = path.relative_to(workspace).as_posix()
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    pairs = [[path, manifest[path]] for path in sorted(manifest)]
    return canonical_digest(pairs)


def assemble_execution_input(
    data: object,
    *,
    workspace: Path,
    model: type[ExecuteInputV1] | type[RunSkillInputV1],
    baseline_tree_id: str | None = None,
) -> ExecuteInputV1 | RunSkillInputV1:
    try:
        return model.model_validate(data)
    except ValidationError:
        pass
    root = validate_input(ExecutionPrepareInputV1, data)
    selected = SelectedTargets(
        **{family: family in root.selected_test_families for family in ("api", "e2e", "fuzz", "performance")}
    )
    mappings: list[dict[str, object]] = []
    for family in root.selected_test_families:
        relative = f"qa/changes/{root.change_id}/codegen/{family}-generated-files.json"
        try:
            document = CodegenAuthoringV1.model_validate(
                _json_document(_regular_input_file(workspace, relative)),
                context={"capability_leafs": leafs_of(root.capability_leafs)},
            )
        except ValidationError as error:
            raise InputError(f"invalid generated-files manifest {relative}: {error}") from error
        if document.change_id != root.change_id or document.layer != family:
            raise InputError(f"generated-files manifest identity mismatch: {relative}")
        mappings.append(document.mapping.model_dump(mode="json"))
    cases = _reviewed_cases(
        workspace,
        change_id=root.change_id,
        capability_leafs=root.capability_leafs,
    )
    case_ids = tuple(
        sorted(
            {
                entry.case_id
                for entry in (*cases.added, *cases.modified)
                if entry.type.lower() in root.selected_test_families
            }
        )
    )
    closed = close_mappings(
        SelectInputV1(
            change_id=root.change_id,
            selected_targets=selected,
            mappings=tuple(mappings),
            reviewed_cases=cases.model_dump(mode="json"),
            capability_leafs=root.capability_leafs,
            case_ids=case_ids,
        )
    )
    locked_baseline = baseline_tree_id or _workspace_tree_id(workspace)
    runner_profile_digest = _RUNNER_PROFILE_DIGEST
    batch_id = canonical_digest(
        {
            "change_id": root.change_id,
            "mapping": closed.model_dump(mode="json"),
            "baseline_tree_id": locked_baseline,
            "runner_profile_digest": runner_profile_digest,
        }
    )
    return model(
        change_id=root.change_id,
        batch_id=batch_id,
        capability_leafs=root.capability_leafs,
        case_ids=case_ids,
        artifact_paths=(),
        mapping=closed,
        selected_targets=selected,
        baseline_tree_id=locked_baseline,
        runner_profile_digest=runner_profile_digest,
    )


def _finalize_payload(
    data: object,
    *,
    workspace: Path,
    model: type[ExecuteInputV1] | type[RunSkillInputV1],
) -> AgentFinalizeInputV1:
    try:
        return AgentFinalizeInputV1.model_validate(data)
    except ValidationError:
        pass
    envelope = validate_input(ExecutionFinalizeRequestV1, data)
    structured = thaw_json(envelope.agent_result.result_payload)
    if not isinstance(structured, dict):
        raise InputError("execution result must be an object")
    baseline_tree_id = structured.get("baseline_tree_id")
    if (
        not isinstance(baseline_tree_id, str)
        or len(baseline_tree_id) != 64
        or any(character not in "0123456789abcdef" for character in baseline_tree_id)
    ):
        raise InputError("execution result baseline_tree_id must be a lowercase SHA-256")
    business = assemble_execution_input(
        envelope.model_dump(mode="json", exclude={"agent_result"}),
        workspace=workspace,
        model=model,
        baseline_tree_id=baseline_tree_id,
    )
    return AgentFinalizeInputV1(
        agent_result=envelope.agent_result,
        **business.model_dump(mode="json"),
    )


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


def _authenticate_files(workspace: Path, declared: tuple[str, ...]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": hashlib.sha256(path.read_bytes()).hexdigest()})
    return artifacts


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.result_payload)


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
    if evidence.selected_targets != payload.selected_targets:
        raise OutputError("execution evidence targets do not match the locked selection")
    if evidence.runner_profile_digest != payload.runner_profile_digest:
        raise OutputError("execution evidence runner profile does not match the locked selection")
    if payload.artifact_paths:
        _authenticate_files(workspace, payload.artifact_paths)
    status = (
        "failed"
        if evidence.receipt.exit_code != 0 or any(item.status == "failed" for item in evidence.results)
        else "passed"
    )
    return evidence.model_copy(
        update={
            "status": status,
            "mapping_digest": mapping_digest(locked),
            "receipt_digest": json_digest(cast(JSONValue, evidence.receipt.model_dump(mode="json"))),
        }
    )


class ExecutePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            binding = validate_binding(request.binding_data)
            business = assemble_execution_input(
                request.input,
                workspace=context.project_root,
                model=ExecuteInputV1,
            )
            return prepare_outcome(
                skill_path=EXECUTE_SKILL,
                business=business,
                binding=binding,
                context=context,
                allowed_outputs=_execution_outputs(business.change_id, run=False),
            )
        except InputError as error:
            return failed_input(error)


class RunPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            binding = validate_binding(request.binding_data)
            business = assemble_execution_input(
                request.input,
                workspace=context.project_root,
                model=RunSkillInputV1,
            )
            return prepare_outcome(
                skill_path=RUN_SKILL,
                business=business,
                binding=binding,
                context=context,
                allowed_outputs=_execution_outputs(business.change_id, run=True),
            )
        except InputError as error:
            return failed_input(error)


class ExecuteFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = _finalize_payload(
                request.input,
                workspace=context.project_root,
                model=ExecuteInputV1,
            )
            evidence = _finalize_evidence(payload, context.project_root)
            write_canonical_evidence(context.write_root, evidence, filename="execute-result.json")
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RunFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = _finalize_payload(
                request.input,
                workspace=context.project_root,
                model=RunSkillInputV1,
            )
            evidence = _finalize_evidence(payload, context.project_root)
            write_canonical_evidence(context.write_root, evidence, filename="run-result.json")
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
