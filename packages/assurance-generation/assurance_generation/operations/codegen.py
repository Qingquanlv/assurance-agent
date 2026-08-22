"""Constructor-closed four-family codegen and API/E2E codegen-fix handlers."""

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
    CodegenFixInputV1,
    CodegenInputV1,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenFixCandidateV1,
    CodegenGeneratedFileAuthoring,
    CodegenMapping,
    CodegenResultV1,
)
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path
from assurance_generation.operations.planning import (
    FAMILIES,
    PLAN_PERSONA,
    Family,
    InputError,
    OutputError,
    closed_family,
    failed_input,
    failed_output,
    leafs_of,
)
from assurance_generation.resource_loader import resource_bytes, resource_text
from assurance_intake.contracts import CaseYamlAuthoring

FIX_FAMILIES: tuple[Family, ...] = ("api", "e2e")
CODEGEN_RESULT_ID = "assurance.generation.result.codegen.v1"
CODEGEN_FIX_RESULT_ID = "assurance.generation.result.codegen-fix.v1"
_RESULT_FILES: Mapping[str, str] = {
    CODEGEN_RESULT_ID: "result-contracts/codegen.v1.schema.json",
    CODEGEN_FIX_RESULT_ID: "result-contracts/codegen-fix.v1.schema.json",
}
_SKILL_FILES: Mapping[Family, str] = {
    "api": "skills/aa-api-codegen/SKILL.md",
    "e2e": "skills/aa-e2e-codegen/SKILL.md",
    "fuzz": "skills/aa-fuzz-codegen/SKILL.md",
    "performance": "skills/aa-performance-codegen/SKILL.md",
}
_FIX_SKILL_FILES: Mapping[Family, str] = {
    "api": "skills/aa-api-codegen-fixer/SKILL.md",
    "e2e": "skills/aa-e2e-codegen-fixer/SKILL.md",
}


def closed_fix_family(family: str) -> Family:
    closed = closed_family(family)
    if closed not in FIX_FAMILIES:
        raise ValueError(f"codegen-fix has no handler for family: {family}")
    return closed


def codegen_result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
    )


def validate_codegen_input(
    data: object, family: Family
) -> tuple[CodegenInputV1, PlanResultV1, CaseYamlAuthoring]:
    try:
        business = CodegenInputV1.model_validate(data)
        leafs = leafs_of(business.capability_leafs)
        plan = PlanResultV1.model_validate(business.reviewed_plan, context={"capability_leafs": leafs})
        cases = CaseYamlAuthoring.model_validate(business.reviewed_cases, context={"capability_leafs": leafs})
    except ValidationError as error:
        raise InputError(str(error)) from error
    if plan.family != family:
        raise InputError(f"reviewed plan family {plan.family!r} does not match {family}")
    return business, plan, cases


def validate_codegen_fix_input(
    data: object, family: Family
) -> tuple[CodegenFixInputV1, PlanResultV1, CaseYamlAuthoring]:
    try:
        business = CodegenFixInputV1.model_validate(data)
        leafs = leafs_of(business.capability_leafs)
        plan = PlanResultV1.model_validate(business.reviewed_plan, context={"capability_leafs": leafs})
        cases = CaseYamlAuthoring.model_validate(business.reviewed_cases, context={"capability_leafs": leafs})
    except ValidationError as error:
        raise InputError(str(error)) from error
    if plan.family != family:
        raise InputError(f"reviewed plan family {plan.family!r} does not match {family}")
    return business, plan, cases


def prepare_codegen_outcome(
    *,
    skill_path: str,
    persona_path: str,
    plan: PlanResultV1,
    cases: CaseYamlAuthoring,
    context_payload: Mapping[str, object],
    binding: AgentBindingDataV1,
    result_schema_id: str,
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(plan.model_dump(mode="json")),
            InstructionPart.from_json(cases.model_dump(mode="json")),
            InstructionPart.from_json(dict(context_payload)),
        ),
        result_contract=codegen_result_contract(result_schema_id),
        execution=binding.execution,
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
    allowed_paths: tuple[str, ...],
) -> tuple[GeneratedFileEntryV1, ...]:
    locked = set(allowed_paths)
    mapped_targets = {item.target_file for item in mapping.entries}
    if not files and mapped_targets:
        raise OutputError("empty files array is invalid when mapping targets exist")
    completed: list[GeneratedFileEntryV1] = []
    listed_test_entries: set[str] = set()
    for entry in files:
        if locked and entry.repo_path not in locked:
            raise OutputError(f"undeclared generated/modified test file: {entry.repo_path}")
        if entry.role == "test_entry" and entry.repo_path not in mapped_targets:
            raise OutputError(f"generated test file is absent from the closed mapping: {entry.repo_path}")
        payload = _workspace_regular_file(workspace, entry.repo_path).read_bytes()
        case_ids = tuple(sorted(entry.case_ids))
        if entry.role == "test_entry" and case_ids != _mapped_case_ids(mapping, entry.repo_path):
            raise OutputError(f"generated test file is absent from the closed mapping: {entry.repo_path}")
        if entry.role != "test_entry" and entry.case_ids:
            raise OutputError(f"{entry.role} entry cannot claim case_ids: {entry.repo_path}")
        if entry.role == "test_entry":
            listed_test_entries.add(entry.repo_path)
        completed.append(
            GeneratedFileEntryV1(
                repo_path=entry.repo_path,
                disposition=entry.disposition,
                role=entry.role,
                case_ids=list(case_ids),
                content_sha256=_digest_bytes(payload),
            )
        )
    for target in sorted(mapped_targets):
        if target not in listed_test_entries:
            raise OutputError(f"codegen mapping is missing: {target}")
    return tuple(sorted(completed, key=lambda item: item.repo_path))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


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
    return document


class CodegenPrepareHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business, plan, cases = validate_codegen_input(request.input, self._family)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            return prepare_codegen_outcome(
                skill_path=_SKILL_FILES[self._family],
                persona_path=PLAN_PERSONA,
                plan=plan,
                cases=cases,
                context_payload={
                    "change_id": business.change_id,
                    "baseline_tree_id": business.baseline_tree_id,
                    "family_constraints": business.family_constraints.model_dump(mode="json"),
                },
                binding=binding,
                result_schema_id=CODEGEN_RESULT_ID,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFinalizeHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = AgentFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, self._family)
            allowed = payload.allowed_paths or payload.artifact_paths
            files = _complete_files(
                context.workspace_root,
                document.files,
                document.mapping,
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
            return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
        except ValidationError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CodegenFixPrepareHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_fix_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business, plan, cases = validate_codegen_fix_input(request.input, self._family)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            return prepare_codegen_outcome(
                skill_path=_FIX_SKILL_FILES[self._family],
                persona_path=PLAN_PERSONA,
                plan=plan,
                cases=cases,
                context_payload={
                    "change_id": business.change_id,
                    "baseline_tree_id": business.baseline_tree_id,
                    "family_constraints": business.family_constraints.model_dump(mode="json"),
                    "allowed_paths": list(business.allowed_paths),
                    "approved_proposal": business.approved_proposal,
                },
                binding=binding,
                result_schema_id=CODEGEN_FIX_RESULT_ID,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFixFinalizeHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_fix_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = AgentFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, self._family)
            allowed = payload.allowed_paths or payload.artifact_paths
            if not allowed:
                raise OutputError("codegen-fix requires an allowed file set")
            if payload.baseline_tree_id is None:
                raise OutputError("codegen-fix requires a baseline tree identity")
            files = _complete_files(
                context.workspace_root,
                document.files,
                document.mapping,
                allowed_paths=allowed,
            )
            result = CodegenFixCandidateV1.model_validate(
                {
                    "schema_version": "1",
                    "change_id": document.change_id,
                    "family": self._family,
                    "baseline_tree_id": payload.baseline_tree_id,
                    "allowed_paths": list(allowed),
                    "files": [item.model_dump(mode="json") for item in files],
                    "mapping": document.mapping.model_dump(mode="json"),
                    "required_capabilities": list(document.required_capabilities),
                },
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
        except ValidationError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def codegen_prepare_handler(family: str) -> TaskHandler:
    return CodegenPrepareHandler(closed_family(family))


def codegen_finalize_handler(family: str) -> TaskHandler:
    return CodegenFinalizeHandler(closed_family(family))


def codegen_fix_prepare_handler(family: str) -> TaskHandler:
    return CodegenFixPrepareHandler(closed_fix_family(family))


def codegen_fix_finalize_handler(family: str) -> TaskHandler:
    return CodegenFixFinalizeHandler(closed_fix_family(family))


__all__ = [
    "CODEGEN_FIX_RESULT_ID",
    "CODEGEN_RESULT_ID",
    "FAMILIES",
    "FIX_FAMILIES",
    "CodegenFinalizeHandler",
    "CodegenFixFinalizeHandler",
    "CodegenFixPrepareHandler",
    "CodegenPrepareHandler",
    "closed_fix_family",
    "codegen_finalize_handler",
    "codegen_fix_finalize_handler",
    "codegen_fix_prepare_handler",
    "codegen_prepare_handler",
    "codegen_result_contract",
]
