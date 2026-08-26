"""Capability-closed four-family plan prepare/finalize handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from pydantic import ValidationError
import yaml

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    FamilyConstraintsV1,
    PlanInputV1,
    under_write_root,
)
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path
from assurance_generation.resource_loader import resource_bytes, resource_text
from assurance_intake.contracts import CaseYamlAuthoring

Family = LayerName
FAMILIES: tuple[Family, ...] = LAYER_NAMES
PrepareKind = Literal["prepare", "finalize"]

PLAN_RESULT_ID = "assurance.generation.result.plan.v1"
PLAN_REVIEW_RESULT_ID = "assurance.generation.result.plan-review.v1"
_RESULT_FILES: Mapping[str, str] = {
    PLAN_RESULT_ID: "result-contracts/plan.v1.schema.json",
    PLAN_REVIEW_RESULT_ID: "result-contracts/plan-review.v1.schema.json",
}
_SKILL_FILES: Mapping[Family, str] = {
    "api": "skills/aa-api-plan/SKILL.md",
    "e2e": "skills/aa-e2e-plan/SKILL.md",
    "fuzz": "skills/aa-fuzz-plan/SKILL.md",
    "performance": "skills/aa-performance-plan/SKILL.md",
}
PLAN_PERSONA = "personas/test-author.md"
REVIEW_PERSONA = "personas/reviewer.md"
_BOUNDED_PROFILES: Mapping[str, str] = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
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


def plan_outputs(change_id: str, family: Family) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{change_id}/plans/{name}" for name in _PLAN_OUTPUT_NAMES[family]))


def plan_review_outputs(change_id: str, family: Family) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                f"qa/changes/{change_id}/review/{family}-plan-review.json",
                f"qa/changes/{change_id}/review/{family}-plan-review-summary.md",
            )
        )
    )


def agent_workspace(
    context: TaskContext,
    *,
    allowed_outputs: tuple[str, ...],
    agent_profile: str,
) -> AgentWorkspaceV1:
    write_root = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


_CASE_TYPES: Mapping[Family, str] = {
    "api": "API",
    "e2e": "E2E",
    "fuzz": "Fuzz",
    "performance": "Performance",
}


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def closed_family(family: str) -> Family:
    if family not in FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return cast(Family, family)


def request_family(request: TaskRequest) -> Family:
    identifiers = tuple(
        identifier
        for identifier in (request.target_capability_id, request.capability_id)
        if identifier is not None
    )
    for identifier in identifiers:
        parts = identifier.split(".")
        for index, part in enumerate(parts[:-1]):
            if part == "generation" and parts[index + 1] in FAMILIES:
                return closed_family(parts[index + 1])
    raise InputError("generation capability does not identify one closed family")


def resolve_family(configured: Family | None, request: TaskRequest) -> Family:
    return request_family(request) if configured is None else configured


def result_contract(
    schema_id: str,
    *,
    capability_leafs: tuple[str, ...] | None = None,
) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    if capability_leafs is not None:
        closed_arrays = 0

        def close_required_capabilities(node: object) -> None:
            nonlocal closed_arrays
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "required_capabilities" and isinstance(value, dict):
                        items = value.get("items")
                        if not isinstance(items, dict):
                            raise InputError("result schema required_capabilities items are missing")
                        items["enum"] = list(capability_leafs)
                        closed_arrays += 1
                    close_required_capabilities(value)
            elif isinstance(node, list):
                for value in node:
                    close_required_capabilities(value)

        close_required_capabilities(payload)
        if closed_arrays == 0:
            raise InputError("result schema required_capabilities are missing")
        if schema_id == PLAN_RESULT_ID:
            definitions = payload.get("$defs")
            scenario = definitions.get("PerformanceScenarioV1") if isinstance(definitions, dict) else None
            scenario_properties = scenario.get("properties") if isinstance(scenario, dict) else None
            capability = (
                scenario_properties.get("capability") if isinstance(scenario_properties, dict) else None
            )
            if not isinstance(capability, dict):
                raise InputError("plan result schema performance capability is missing")
            capability["enum"] = list(capability_leafs)
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
        schema_document=payload,
    )


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def leafs_of(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(values)


def _change_root(workspace: Path, change_id: str) -> Path:
    if (
        not change_id
        or change_id in {".", ".."}
        or "/" in change_id
        or "\\" in change_id
        or PurePosixPath(change_id).name != change_id
    ):
        raise InputError("change_id must be one canonical path component")
    root = workspace / "qa" / "changes" / change_id
    try:
        root.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("change workspace escapes the attempt workspace") from error
    return root


def _regular_input_file(workspace: Path, path: Path) -> Path:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("case input escapes the attempt workspace") from error
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise InputError(f"case input is not a regular single-link file: {path}")
    return path


def _yaml_document(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise InputError(f"case input is not valid YAML: {path}") from error
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise InputError(f"case input must be a string-keyed mapping: {path}")
    return cast(dict[str, Any], document)


def load_family_cases(
    workspace: Path,
    *,
    change_id: str,
    family: Family,
    capability_leafs: tuple[str, ...],
) -> CaseYamlAuthoring:
    cases_root = _change_root(workspace, change_id) / "cases"
    if not cases_root.is_dir() or cases_root.is_symlink():
        raise InputError(f"reviewed case directory is missing: qa/changes/{change_id}/cases")
    selected: dict[str, list[object]] = {"added": [], "modified": []}
    schema_versions: set[str] = set()
    paths = tuple(sorted(cases_root.glob("**/case.yaml"), key=lambda item: item.as_posix()))
    if not paths:
        raise InputError(f"reviewed case files are missing: qa/changes/{change_id}/cases/**/case.yaml")
    for path in paths:
        document = _yaml_document(_regular_input_file(workspace, path))
        version = document.get("schema_version")
        if isinstance(version, str) and version.strip():
            schema_versions.add(version)
        for section in ("added", "modified"):
            entries = document.get(section)
            if not isinstance(entries, list):
                raise InputError(f"{path}: {section} must be a list")
            selected[section].extend(
                entry
                for entry in entries
                if isinstance(entry, Mapping) and entry.get("type") == _CASE_TYPES[family]
            )
    if len(schema_versions) != 1:
        raise InputError("reviewed case files must use one non-empty schema_version")
    payload = {
        "schema_version": next(iter(schema_versions)),
        "added": selected["added"],
        "modified": selected["modified"],
        "removed": [],
    }
    try:
        return CaseYamlAuthoring.model_validate(
            payload,
            context={"capability_leafs": leafs_of(capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error


def constraints_for_cases(*, family: Family, change_id: str, cases: CaseYamlAuthoring) -> FamilyConstraintsV1:
    entries = tuple((*cases.added, *cases.modified))
    return FamilyConstraintsV1(
        write_roots=(f"qa/changes/{change_id}/plans/",),
        operations=tuple(entry.test_condition_id for entry in entries),
        risks=tuple(entry.risk.level for entry in entries),
    )


def validate_plan_input(
    data: object,
    *,
    family: Family,
    workspace: Path,
) -> tuple[PlanInputV1, CaseYamlAuthoring]:
    try:
        business = PlanInputV1.model_validate(data)
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
                context={"capability_leafs": leafs_of(business.capability_leafs)},
            )
        except ValidationError as error:
            raise InputError(str(error)) from error
    constraints = business.family_constraints or constraints_for_cases(
        family=family,
        change_id=business.change_id,
        cases=cases,
    )
    business = business.model_copy(
        update={
            "reviewed_cases": cases.model_dump(mode="json"),
            "family_constraints": constraints,
        }
    )
    return business, cases


def prepare_plan_outcome(
    *,
    family: Family,
    skill_path: str,
    persona_path: str,
    business: PlanInputV1,
    cases: CaseYamlAuthoring,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
    close_result_capabilities: bool = False,
) -> TaskOutcome:
    del family
    if business.family_constraints is None:
        raise InputError("family_constraints were not materialized")
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(cases.model_dump(mode="json")),
            InstructionPart.from_json(business.family_constraints.model_dump(mode="json")),
        ),
        result_contract=result_contract(
            result_schema_id,
            capability_leafs=business.capability_leafs if close_result_capabilities else None,
        ),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _workspace_file(workspace: Path, relative: str) -> Path:
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
    if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> None:
    for relative in declared:
        if not under_write_root(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")


class PlanPrepareHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            business, cases = validate_plan_input(
                request.input,
                family=family,
                workspace=context.project_root,
            )
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            return prepare_plan_outcome(
                family=family,
                skill_path=_SKILL_FILES[family],
                persona_path=PLAN_PERSONA,
                business=business,
                cases=cases,
                binding=binding,
                result_schema_id=PLAN_RESULT_ID,
                context=context,
                allowed_outputs=plan_outputs(business.change_id, family),
                close_result_capabilities=True,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class PlanFinalizeHandler:
    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            payload = AgentFinalizeInputV1.model_validate(request.input)
            try:
                document = PlanResultV1.model_validate(
                    _structured(payload),
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.family != family:
                raise OutputError(f"plan family {document.family!r} does not match {family}")
            if payload.artifact_paths:
                _authenticate_files(context.write_root, document.output_files, payload.artifact_paths)
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except (InputError, ValidationError) as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def planning_handler(family: str, kind: PrepareKind) -> TaskHandler:
    closed = closed_family(family)
    if kind == "prepare":
        return PlanPrepareHandler(closed)
    if kind == "finalize":
        return PlanFinalizeHandler(closed)
    raise ValueError(f"unknown planning handler kind: {kind}")


__all__ = [
    "FAMILIES",
    "PLAN_PERSONA",
    "PLAN_RESULT_ID",
    "PLAN_REVIEW_RESULT_ID",
    "REVIEW_PERSONA",
    "Family",
    "InputError",
    "OutputError",
    "PlanFinalizeHandler",
    "PlanPrepareHandler",
    "closed_family",
    "failed_input",
    "failed_output",
    "leafs_of",
    "planning_handler",
    "prepare_plan_outcome",
    "request_family",
    "resolve_family",
    "result_contract",
    "validate_plan_input",
]
