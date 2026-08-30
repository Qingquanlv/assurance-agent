"""Capability-closed four-family codegen and API/E2E codegen-fix handlers."""

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
    FamilyConstraintsV1,
    under_write_root,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenFixCandidateV1,
    CodegenGeneratedFileAuthoring,
    CodegenMapping,
    CodegenResultV1,
    CodegenResultV2,
    family_allows_target,
    staged_generated_path,
)
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path
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
    load_family_cases,
    resolve_family,
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
_PLAN_OUTPUT_NAMES: Mapping[Family, tuple[str, ...]] = {
    "api": (
        "api-plan.md",
        "api-test-data-plan.md",
        "api-codegen-plan.md",
        "api-codegen-mapping.json",
        "m3-review-summary.md",
    ),
    "e2e": (
        "e2e-plan.md",
        "e2e-test-data-plan.md",
        "e2e-codegen-plan.md",
        "e2e-codegen-mapping.json",
        "m4-review-summary.md",
    ),
    "fuzz": (
        "fuzz-plan.md",
        "fuzz-codegen-plan.md",
        "fuzz-codegen-mapping.json",
        "fuzz-review-summary.md",
    ),
    "performance": (
        "performance-plan.md",
        "performance-codegen-plan.md",
        "performance-codegen-mapping.json",
        "performance-review-summary.md",
    ),
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
        schema_document=payload,
    )


def _case_capability_keys(case: object) -> tuple[str, ...]:
    trace = getattr(case, "trace", None)
    if not isinstance(trace, Mapping):
        return ()
    return tuple(sorted(key for key in trace if isinstance(key, str)))


def _first_fuzz_endpoint(cases: CaseYamlAuthoring) -> tuple[str, str]:
    for case in (*cases.added, *cases.modified):
        fuzz = case.automation.fuzz
        if fuzz is None:
            continue
        first = fuzz.endpoints[0]
        return f"{first.method} {first.path}", fuzz.property
    raise InputError("fuzz cases do not declare an endpoint/property strategy")


def plan_from_cases(
    *,
    family: Family,
    change_id: str,
    cases: CaseYamlAuthoring,
    capability_leafs: tuple[str, ...],
) -> PlanResultV1:
    entries = tuple(sorted((*cases.added, *cases.modified), key=lambda item: item.case_id))
    required = tuple(sorted({key for case in entries for key in _case_capability_keys(case)}))
    document: dict[str, object] = {
        "schema_version": "1",
        "family": family,
        "change_id": change_id,
        "case_ids": [case.case_id for case in entries],
        "required_capabilities": list(required),
        "coverage": [
            {
                "case_id": case.case_id,
                "operation": case.test_condition_id,
                "risk": case.risk.level,
                "required_capabilities": list(_case_capability_keys(case)),
            }
            for case in entries
        ],
        "output_files": [f"qa/changes/{change_id}/plans/{name}" for name in _PLAN_OUTPUT_NAMES[family]],
    }
    if family == "fuzz":
        endpoint, property_name = _first_fuzz_endpoint(cases)
        document["fuzz_strategy"] = {
            "endpoint": endpoint,
            "property_name": property_name,
        }
    if family == "performance":
        scenarios: list[dict[str, object]] = []
        for case in entries:
            performance = case.automation.performance
            if performance is None:
                raise InputError(f"performance case {case.case_id} has no typed scenario")
            scenario = performance.scenario
            scenarios.append(
                {
                    "scenario_id": case.case_id.lower(),
                    "capability": scenario.capability,
                    "endpoint": scenario.endpoint,
                    "p95_ms": scenario.thresholds.p95_ms,
                    "error_rate_max": scenario.thresholds.error_rate_max,
                }
            )
        document["performance_scenarios"] = scenarios
    try:
        return PlanResultV1.model_validate(
            document,
            context={"capability_leafs": leafs_of(capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error


def validate_codegen_input(
    data: object,
    family: Family,
    workspace: Path,
) -> tuple[CodegenInputV1, PlanResultV1, CaseYamlAuthoring]:
    try:
        business = CodegenInputV1.model_validate(data)
        leafs = leafs_of(business.capability_leafs)
    except ValidationError as error:
        raise InputError(str(error)) from error
    if business.reviewed_cases is None:
        cases = load_family_cases(
            workspace,
            change_id=business.change_id,
            family=family,
            capability_leafs=business.capability_leafs,
        )
    else:
        try:
            cases = CaseYamlAuthoring.model_validate(
                business.reviewed_cases,
                context={"capability_leafs": leafs},
            )
        except ValidationError as error:
            raise InputError(str(error)) from error
    if business.reviewed_plan is None:
        plan = plan_from_cases(
            family=family,
            change_id=business.change_id,
            cases=cases,
            capability_leafs=business.capability_leafs,
        )
    else:
        try:
            plan = PlanResultV1.model_validate(
                business.reviewed_plan,
                context={"capability_leafs": leafs},
            )
        except ValidationError as error:
            raise InputError(str(error)) from error
    if plan.family != family:
        raise InputError(f"reviewed plan family {plan.family!r} does not match {family}")
    if plan.change_id != business.change_id:
        raise InputError(
            f"reviewed plan change_id {plan.change_id!r} does not match business change_id "
            f"{business.change_id!r}"
        )
    constraints: FamilyConstraintsV1 = business.family_constraints or constraints_for_cases(
        family=family,
        change_id=business.change_id,
        cases=cases,
    )
    business = business.model_copy(
        update={
            "reviewed_plan": plan.model_dump(mode="json"),
            "reviewed_cases": cases.model_dump(mode="json"),
            "family_constraints": constraints,
        }
    )
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
    if business.family_constraints is None or business.baseline_tree_id is None:
        raise InputError("codegen-fix requires family_constraints and baseline_tree_id")
    if plan.family != family:
        raise InputError(f"reviewed plan family {plan.family!r} does not match {family}")
    if plan.change_id != business.change_id:
        raise InputError(
            f"reviewed plan change_id {plan.change_id!r} does not match business change_id "
            f"{business.change_id!r}"
        )
    return business, plan, cases


def codegen_outputs(
    change_id: str,
    family: Family,
    *,
    fix: bool = False,
    mapping_targets: tuple[str, ...] = (),
) -> tuple[str, ...]:
    suffix = "-fix" if fix else ""
    ordinary = (
        f"qa/changes/{change_id}/codegen/{family}-codegen{suffix}-summary.md",
        f"qa/changes/{change_id}/codegen/{family}-generated-files.json",
    )
    staged = tuple(staged_generated_path(change_id, family, target) for target in mapping_targets)
    return tuple(sorted((*ordinary, *staged)))


def _closed_mapping_targets(workspace: Path, plan: PlanResultV1, family: Family) -> tuple[str, ...]:
    mapping_name = f"{family}-codegen-mapping.json"
    relatives = tuple(path for path in plan.output_files if PurePosixPath(path).name == mapping_name)
    if not relatives:
        relatives = (f"qa/changes/{plan.change_id}/plans/{mapping_name}",)
    for relative in relatives:
        try:
            canonical_relative_path(relative)
        except ValueError as error:
            raise InputError(str(error)) from error
        path = workspace.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            mapping = CodegenMapping.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise InputError(f"closed codegen mapping is invalid: {relative}: {error}") from error
        if mapping.layer != family:
            raise InputError(f"closed mapping layer {mapping.layer!r} does not match {family}")
        return tuple(sorted({item.target_file for item in mapping.entries}))
    return ()


def prepare_codegen_outcome(
    *,
    skill_path: str,
    persona_path: str,
    plan: PlanResultV1,
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
            InstructionPart.from_json(plan.model_dump(mode="json")),
            InstructionPart.from_json(cases.model_dump(mode="json")),
            InstructionPart.from_json(dict(context_payload)),
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
            staged = staged_generated_path(change_id, family, target)
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


def _authenticate_manifest(
    workspace: Path,
    *,
    document: CodegenAuthoringV1,
    capability_leafs: tuple[str, ...],
) -> None:
    relative = f"qa/changes/{document.change_id}/codegen/{document.layer}-generated-files.json"
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
        return
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
            business, plan, cases = validate_codegen_input(
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
                "generated_files_root": f"qa/changes/{business.change_id}/generated/{family}/files",
            }
            if business.baseline_tree_id is not None:
                context_payload["baseline_tree_id"] = business.baseline_tree_id
            return prepare_codegen_outcome(
                skill_path=_SKILL_FILES[family],
                persona_path=PLAN_PERSONA,
                plan=plan,
                cases=cases,
                context_payload=context_payload,
                binding=binding,
                result_schema_id=CODEGEN_RESULT_ID,
                context=context,
                scope_id=business.change_id,
                allowed_outputs=codegen_outputs(
                    business.change_id,
                    family,
                    mapping_targets=_closed_mapping_targets(context.project_root, plan, family),
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFinalizeHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            payload = AgentFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, family)
            allowed = payload.allowed_paths or payload.artifact_paths
            files = _complete_files(
                context.project_root,
                document.files,
                document.mapping,
                change_id=document.change_id,
                family=family,
                allowed_paths=allowed,
            )
            if family in FIX_FAMILIES:
                structured = _structured(payload)
                verdict = "accepted"
                repair = None
                if isinstance(structured, dict) and structured.get("verdict") == "needs_fix":
                    verdict = "needs_fix"
                    repair = structured.get("repair")
                result = CodegenResultV2.model_validate(
                    {
                        "schema_version": "2",
                        "verdict": verdict,
                        "change_id": document.change_id,
                        "layer": document.layer,
                        "files": [item.model_dump(mode="json") for item in files],
                        "mapping": document.mapping.model_dump(mode="json"),
                        "required_capabilities": list(document.required_capabilities),
                        "repair": repair,
                    },
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            else:
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
                context.project_root,
                document=document,
                capability_leafs=payload.capability_leafs,
            )
            return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
        except (InputError, ValidationError) as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CodegenFixPrepareHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_fix_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = closed_fix_family(resolve_family(self._family, request))
            business, plan, cases = validate_codegen_fix_input(request.input, family)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            if business.family_constraints is None or business.baseline_tree_id is None:
                raise InputError("codegen-fix inputs were not materialized")
            return prepare_codegen_outcome(
                skill_path=_FIX_SKILL_FILES[family],
                persona_path=PLAN_PERSONA,
                plan=plan,
                cases=cases,
                context_payload={
                    "change_id": business.change_id,
                    "baseline_tree_id": business.baseline_tree_id,
                    "family_constraints": business.family_constraints.model_dump(mode="json"),
                    "allowed_paths": list(business.allowed_paths),
                    "approved_proposal": business.approved_proposal,
                    "generated_files_root": f"qa/changes/{business.change_id}/generated/{family}/files",
                },
                binding=binding,
                result_schema_id=CODEGEN_FIX_RESULT_ID,
                context=context,
                scope_id=business.change_id,
                allowed_outputs=codegen_outputs(
                    business.change_id,
                    family,
                    fix=True,
                    mapping_targets=tuple(business.allowed_paths),
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFixFinalizeHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_fix_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = closed_fix_family(resolve_family(self._family, request))
            payload = AgentFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, family)
            allowed = payload.allowed_paths or payload.artifact_paths
            if not allowed:
                raise OutputError("codegen-fix requires an allowed file set")
            if payload.baseline_tree_id is None:
                raise OutputError("codegen-fix requires a baseline tree identity")
            files = _complete_files(
                context.project_root,
                document.files,
                document.mapping,
                change_id=document.change_id,
                family=family,
                allowed_paths=allowed,
            )
            result = CodegenFixCandidateV1.model_validate(
                {
                    "schema_version": "1",
                    "change_id": document.change_id,
                    "family": family,
                    "baseline_tree_id": payload.baseline_tree_id,
                    "allowed_paths": list(allowed),
                    "files": [item.model_dump(mode="json") for item in files],
                    "mapping": document.mapping.model_dump(mode="json"),
                    "required_capabilities": list(document.required_capabilities),
                },
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            _authenticate_manifest(
                context.project_root,
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
    "codegen_outputs",
    "codegen_prepare_handler",
    "codegen_result_contract",
]
