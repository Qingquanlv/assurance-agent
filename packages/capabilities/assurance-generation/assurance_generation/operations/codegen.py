"""Capability-closed four-family codegen handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    CodegenInputV1,
    FamilyConstraintsV1,
    under_write_root,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenGeneratedFileAuthoring,
    CodegenMapping,
    CodegenResultV1,
    CodegenScopeV1,
    durable_test_path,
    family_allows_target,
)
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import canonical_relative_path
from assurance_generation.operations.codegen_scope import build_codegen_scope
from assurance_generation.operations.planning import (
    FAMILIES,
    PLAN_PERSONA,
    Family,
    InputError,
    OutputError,
    agent_workspace,
    closed_family,
    failed_input,
    failed_output,
    leafs_of,
    constraints_for_cases,
    load_family_case_modules,
    resolve_family,
)
from assurance_generation.operations.resolve_inputs import authenticate_reviewed_case
from assurance_generation.resource_loader import resource_bytes, resource_text
from assurance_intake.contracts import CaseYamlAuthoring

CODEGEN_RESULT_ID = "assurance.generation.result.codegen.v1"
_RESULT_FILES: Mapping[str, str] = {
    CODEGEN_RESULT_ID: "result-contracts/codegen.v1.schema.json",
}
_SKILL_FILES: Mapping[Family, str] = {
    "api": "skills/aa-api-codegen/SKILL.md",
    "e2e": "skills/aa-e2e-codegen/SKILL.md",
    "fuzz": "skills/aa-fuzz-codegen/SKILL.md",
    "performance": "skills/aa-performance-codegen/SKILL.md",
}


def codegen_result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        delivery_mode="assistant_json_local_v1",
        schema_document=payload,
    )


def validate_codegen_input(
    data: object,
    family: Family,
    workspace: Path,
) -> tuple[CodegenInputV1, CodegenScopeV1, CaseYamlAuthoring]:
    try:
        business = CodegenInputV1.model_validate(data)
        leafs = leafs_of(business.capability_leafs)
    except ValidationError as error:
        raise InputError(str(error)) from error
    if business.reviewed_case is not None:
        try:
            reviewed = authenticate_reviewed_case(
                business.reviewed_case,
                workspace,
                change_id=business.change_id,
                coverage_epoch=business.coverage_epoch,
            )
        except ValueError as error:
            raise InputError(str(error)) from error
        case_paths = tuple(item.path for item in reviewed.case_refs)
    else:
        case_paths = None
    cases, case_ids_by_path = load_family_case_modules(
        workspace,
        change_id=business.change_id,
        family=family,
        capability_leafs=business.capability_leafs,
        case_paths=case_paths,
    )
    if business.reviewed_cases is not None:
        try:
            CaseYamlAuthoring.model_validate(
                business.reviewed_cases,
                context={"capability_leafs": leafs},
            )
        except ValidationError as error:
            raise InputError(str(error)) from error
    try:
        scope = build_codegen_scope(
            family=family,
            change_id=business.change_id,
            cases=cases,
            capability_leafs=leafs,
            case_ids_by_path=case_ids_by_path,
        )
    except ValueError as error:
        raise InputError(str(error)) from error
    constraints: FamilyConstraintsV1 = business.family_constraints or constraints_for_cases(
        family=family,
        change_id=business.change_id,
        cases=cases,
    )
    business = business.model_copy(
        update={
            "codegen_scope": scope.model_dump(mode="json"),
            "reviewed_cases": cases.model_dump(mode="json"),
            "family_constraints": constraints,
        }
    )
    return business, scope, cases


def codegen_outputs(scope: CodegenScopeV1) -> tuple[str, ...]:
    return scope.locked_outputs


def prepare_codegen_outcome(
    *,
    skill_path: str,
    persona_path: str,
    scope: CodegenScopeV1,
    cases: CaseYamlAuthoring,
    context_payload: Mapping[str, object],
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    scope_id: str,
    allowed_outputs: tuple[str, ...],
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(scope.model_dump(mode="json")),
            InstructionPart.from_json(cases.model_dump(mode="json")),
            InstructionPart.from_json({**context_payload, "allowed_outputs": list(allowed_outputs)}),
        ),
        result_contract=codegen_result_contract(result_schema_id),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
            scope_id=scope_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _workspace_regular_file(workspace: Path, relative: str) -> Path:
    try:
        canonical_relative_path(relative)
    except ValueError as error:
        raise OutputError(str(error)) from error
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


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _mapped_case_ids(mapping: CodegenMapping, path: str) -> tuple[str, ...]:
    return tuple(sorted(item.case_id for item in mapping.entries if item.target_file == path))


def _complete_files(
    workspace: Path,
    files: tuple[CodegenGeneratedFileAuthoring, ...],
    mapping: CodegenMapping,
    *,
    change_id: str,
    family: Family,
    allowed_paths: tuple[str, ...],
) -> tuple[GeneratedFileEntryV1, ...]:
    del change_id
    mapped_targets = {item.target_file for item in mapping.entries}
    if not files and mapped_targets:
        raise OutputError("empty files array is invalid when mapping targets exist")
    completed: list[GeneratedFileEntryV1] = []
    listed_test_entries: set[str] = set()
    for entry in files:
        target = entry.repo_path
        if not family_allows_target(family, target):
            raise OutputError(f"generated target is outside family policy: {target}")
        if allowed_paths and not under_write_root(target, allowed_paths):
            raise OutputError(f"undeclared generated/modified test file: {target}")
        if entry.role == "test_entry" and target not in mapped_targets:
            raise OutputError(f"generated test file is absent from the closed mapping: {target}")
        try:
            staged = durable_test_path(target)
        except ValueError as error:
            raise OutputError(str(error)) from error
        payload = _workspace_regular_file(workspace, staged).read_bytes()
        case_ids = tuple(sorted(entry.case_ids))
        if entry.role == "test_entry" and case_ids != _mapped_case_ids(mapping, target):
            raise OutputError(f"generated test file is absent from the closed mapping: {target}")
        if entry.role != "test_entry" and entry.case_ids:
            raise OutputError(f"{entry.role} entry cannot claim case_ids: {target}")
        if entry.role == "test_entry":
            listed_test_entries.add(target)
        completed.append(
            GeneratedFileEntryV1(
                repo_path=target,
                disposition=entry.disposition,
                role=entry.role,
                case_ids=list(case_ids),
                content_sha256=_digest_bytes(payload),
            )
        )
    for target in sorted(mapped_targets):
        if not family_allows_target(family, target):
            raise OutputError(f"generated target is outside family policy: {target}")
        if target not in listed_test_entries:
            raise OutputError(f"codegen mapping is missing: {target}")
    return tuple(sorted(completed, key=lambda item: item.repo_path))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.result_payload)


def _finalize_authoring(payload: AgentFinalizeInputV1, family: Family) -> CodegenAuthoringV1:
    try:
        document = CodegenAuthoringV1.model_validate(
            _structured(payload),
            context={"capability_leafs": leafs_of(payload.capability_leafs)},
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if document.layer != family:
        raise OutputError(f"codegen family {document.layer!r} does not match {family}")
    if payload.change_id is not None and document.change_id != payload.change_id:
        raise OutputError("codegen change_id does not match locked change_id")
    if payload.required_capabilities is None:
        raise InputError("host codegen scope required_capabilities are missing")
    if tuple(document.required_capabilities) != payload.required_capabilities:
        raise OutputError("required_capabilities must exactly match the host codegen scope")
    mapped_ids = tuple(sorted(item.case_id for item in document.mapping.entries))
    if payload.scope_case_ids is None:
        raise InputError("host codegen scope case_ids are missing")
    if mapped_ids != payload.scope_case_ids:
        raise OutputError("mapping case IDs must exactly match the host codegen scope")
    if payload.reviewed_mapping is not None:
        try:
            reviewed_mapping = CodegenMapping.model_validate(payload.reviewed_mapping)
        except ValidationError as error:
            raise InputError(f"reviewed plan mapping is invalid: {error}") from error
        if document.mapping != reviewed_mapping:
            raise OutputError("mapping must exactly match the reviewed plan")
    return document


def _authenticate_manifest(
    workspace: Path,
    *,
    document: CodegenAuthoringV1,
    capability_leafs: tuple[str, ...],
) -> None:
    relative = f"qa/results/codegen/{document.layer}-generated-files.json"
    try:
        canonical_relative_path(relative)
    except ValueError as error:
        raise OutputError(str(error)) from error
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError("generated-files manifest escapes the attempt workspace") from error
    if not path.exists():
        raise OutputError(f"generated-files manifest is missing: {relative}")
    path = _workspace_regular_file(workspace, relative)
    try:
        manifest = CodegenAuthoringV1.model_validate(
            json.loads(path.read_text(encoding="utf-8")),
            context={"capability_leafs": leafs_of(capability_leafs)},
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise OutputError(f"generated-files manifest is invalid: {relative}: {error}") from error
    if manifest != document:
        raise OutputError("generated-files manifest does not match the structured codegen result")


class CodegenPrepareHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            business, scope, cases = validate_codegen_input(
                request.input,
                family,
                context.project_root,
            )
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            if business.family_constraints is None:
                raise InputError("family_constraints were not materialized")
            context_payload: dict[str, object] = {
                "change_id": business.change_id,
                "family_constraints": business.family_constraints.model_dump(mode="json"),
                "generated_files_root": "qa/tests",
                "codegen_scope": scope.model_dump(mode="json"),
            }
            return prepare_codegen_outcome(
                skill_path=_SKILL_FILES[family],
                persona_path=PLAN_PERSONA,
                scope=scope,
                cases=cases,
                context_payload=context_payload,
                binding=binding,
                result_schema_id=CODEGEN_RESULT_ID,
                context=context,
                scope_id=business.change_id,
                allowed_outputs=codegen_outputs(scope),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFinalizeHandler:
    input_model = AgentFinalizeInputV1

    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            payload = AgentFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, family)
            allowed = payload.allowed_paths or payload.artifact_paths
            files = _complete_files(
                context.write_root,
                document.files,
                document.mapping,
                change_id=document.change_id,
                family=family,
                allowed_paths=allowed,
            )
            result = CodegenResultV1.model_validate(
                {
                    "schema_version": "1",
                    "change_id": document.change_id,
                    "layer": document.layer,
                    "files": [item.model_dump(mode="json") for item in files],
                    "mapping": document.mapping.model_dump(mode="json"),
                    "required_capabilities": list(document.required_capabilities),
                },
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            _authenticate_manifest(
                context.write_root,
                document=document,
                capability_leafs=payload.capability_leafs,
            )
            return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
        except (InputError, ValidationError) as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def codegen_prepare_handler(family: str) -> TaskHandler:
    return CodegenPrepareHandler(closed_family(family))


def codegen_finalize_handler(family: str) -> TaskHandler:
    return CodegenFinalizeHandler(closed_family(family))


__all__ = [
    "CODEGEN_RESULT_ID",
    "FAMILIES",
    "CodegenFinalizeHandler",
    "CodegenPrepareHandler",
    "codegen_finalize_handler",
    "codegen_outputs",
    "codegen_prepare_handler",
    "codegen_result_contract",
]
