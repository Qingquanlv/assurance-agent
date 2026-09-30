"""Capability-closed four-family plan prepare/finalize handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from pydantic import ValidationError
import yaml

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    agent_run_request,
    failed_input,
    failed_output,
    result_contract_from,
)
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import (
    AgentFinalizeInputV1,
    FamilyConstraintsV1,
    PlanInputV1,
    under_write_root,
)
from assurance_generation.contracts.codegen import CodegenMapping, family_allows_target
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path
from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.resource_loader import resource_bytes, resource_text
from assurance_intake.contracts import (
    CaseYamlAuthoring,
    EvidenceArtifactRefV1,
    LoopRoundHistoryV1,
)
from assurance_intake.operations.planning_facts import build_planning_facts
from assurance_intake.operations.loop_history import build_loop_round_history
from assurance_generation.operations.resolve_inputs import authenticate_reviewed_case
from assurance_generation.operations.plan_consistency import check_plan_consistency

Family = LayerName
FAMILIES: tuple[Family, ...] = LAYER_NAMES
PrepareKind = Literal["prepare", "finalize"]

PLAN_RESULT_ID = "assurance.generation.result.plan.v1"
PLAN_REVIEW_RESULT_ID = "assurance.generation.result.codegen-review.v1"
_RESULT_FILES: Mapping[str, str] = {
    PLAN_RESULT_ID: "result-contracts/plan.v1.schema.json",
    PLAN_REVIEW_RESULT_ID: "result-contracts/plan-review.v1.schema.json",
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
    del change_id
    return tuple(sorted(f"qa/results/plans/{name}" for name in _PLAN_OUTPUT_NAMES[family]))


def plan_review_outputs(
    change_id: str,
    family: Family,
    *,
    coverage_epoch: int = 0,
    review_round: int = 0,
) -> tuple[str, ...]:
    del change_id, coverage_epoch, review_round
    return tuple(
        sorted(
            (
                f"qa/results/review/{family}-codegen-review.json",
                f"qa/results/review/{family}-codegen-review-summary.md",
            )
        )
    )


_CASE_TYPES: Mapping[Family, str] = {
    "api": "API",
    "e2e": "E2E",
    "fuzz": "Fuzz",
    "performance": "Performance",
}


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
    return result_contract_from(schema_id, payload)


def evidence_ref(workspace: Path, relative: str) -> EvidenceArtifactRefV1:
    data = _regular_input_file(
        workspace,
        workspace.joinpath(*PurePosixPath(relative).parts),
        label="loop evidence",
    ).read_bytes()
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def expected_plan_review_history(
    *,
    change_id: str,
    coverage_epoch: int,
    family: Family,
    round_index: int,
    outcome: str,
    input_refs: tuple[EvidenceArtifactRefV1, ...],
    source_refs: tuple[EvidenceArtifactRefV1, ...],
) -> LoopRoundHistoryV1:
    ordered_inputs = tuple(sorted(input_refs, key=lambda item: item.path))
    review_input_digest = engine_digest(
        cast(JSONValue, [item.model_dump(mode="json") for item in ordered_inputs])
    )
    return build_loop_round_history(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        loop_kind="plan_review",
        family=family,
        round_index=round_index,
        outcome=outcome,
        review_input_digest=review_input_digest,
        source_refs=tuple(sorted(source_refs, key=lambda item: item.path)),
    )


def authenticate_loop_round_history(
    context: TaskContext,
    *,
    relative: str,
    expected: LoopRoundHistoryV1,
) -> EvidenceArtifactRefV1:
    path = context.write_root.joinpath(*PurePosixPath(relative).parts)
    try:
        authored = LoopRoundHistoryV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise OutputError(f"invalid plan-review history: {error}") from error
    if authored != expected:
        raise OutputError("plan-review history does not match the locked review inputs")
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(path.read_bytes()).hexdigest())


def leafs_of(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(values)


def validate_reviewed_plan(business: PlanInputV1, family: Family, cases: CaseYamlAuthoring) -> PlanResultV1:
    try:
        plan = PlanResultV1.model_validate(
            business.reviewed_plan,
            context={"capability_leafs": leafs_of(business.capability_leafs)},
        )
        if plan.family != family:
            raise ValueError(f"reviewed plan family {plan.family!r} does not match {family}")
        if plan.change_id != business.change_id:
            raise ValueError(
                f"reviewed plan change_id {plan.change_id!r} does not match business change_id "
                f"{business.change_id!r}"
            )
        plan.require_case_scope(cases)
    except ValueError as error:
        raise InputError(str(error)) from error
    return plan


def _change_root(workspace: Path, change_id: str) -> Path:
    if (
        not change_id
        or change_id in {".", ".."}
        or "/" in change_id
        or "\\" in change_id
        or PurePosixPath(change_id).name != change_id
    ):
        raise InputError("change_id must be one canonical path component")
    root = workspace / "qa"
    try:
        root.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("change workspace escapes the attempt workspace") from error
    return root


def _regular_input_file(workspace: Path, path: Path, *, label: str = "case input") -> Path:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError(f"{label} escapes the attempt workspace") from error
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise InputError(f"{label} is not a regular single-link file: {path}")
    return path


def _yaml_document(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise InputError(f"case input is not valid YAML: {path}") from error
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise InputError(f"case input must be a string-keyed mapping: {path}")
    return cast(dict[str, Any], document)


def load_family_case_modules(
    workspace: Path,
    *,
    change_id: str,
    family: Family,
    capability_leafs: tuple[str, ...],
    case_paths: tuple[str, ...] | None = None,
) -> tuple[CaseYamlAuthoring, dict[str, tuple[str, ...]]]:
    _change_root(workspace, change_id)
    cases_root = workspace / "qa" / "cases"
    try:
        cases_root.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("reviewed case directory escapes the attempt workspace") from error
    if not cases_root.is_dir() or cases_root.is_symlink():
        raise InputError("reviewed case directory is missing: qa/cases")
    selected: dict[str, list[object]] = {"added": [], "modified": []}
    schema_versions: set[str] = set()
    grouped: dict[str, list[str]] = {}
    paths = (
        tuple(workspace.joinpath(*PurePosixPath(path).parts) for path in case_paths)
        if case_paths is not None
        else tuple(sorted(cases_root.glob("**/case.yaml"), key=lambda item: item.as_posix()))
    )
    if not paths:
        raise InputError("reviewed case files are missing: qa/cases/**/case.yaml")
    relatives = (
        case_paths
        if case_paths is not None
        else tuple(path.relative_to(workspace).as_posix() for path in paths)
    )
    for path, relative in zip(paths, relatives, strict=True):
        document = _yaml_document(_regular_input_file(workspace, path))
        version = document.get("schema_version")
        if isinstance(version, str) and version.strip():
            schema_versions.add(version)
        ids: list[str] = []
        for section in ("added", "modified"):
            entries = document.get(section)
            if not isinstance(entries, list):
                raise InputError(f"{path}: {section} must be a list")
            for entry in entries:
                if not (isinstance(entry, Mapping) and entry.get("type") == _CASE_TYPES[family]):
                    continue
                selected[section].append(entry)
                case_id = entry.get("case_id")
                if isinstance(case_id, str):
                    ids.append(case_id)
        grouped[relative] = ids
    if len(schema_versions) != 1:
        raise InputError("reviewed case files must use one non-empty schema_version")
    payload = {
        "schema_version": next(iter(schema_versions)),
        "added": selected["added"],
        "modified": selected["modified"],
        "removed": [],
    }
    try:
        cases = CaseYamlAuthoring.model_validate(
            payload,
            context={"capability_leafs": leafs_of(capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error
    return cases, {path: tuple(ids) for path, ids in grouped.items()}


def load_family_cases(
    workspace: Path,
    *,
    change_id: str,
    family: Family,
    capability_leafs: tuple[str, ...],
    case_paths: tuple[str, ...] | None = None,
) -> CaseYamlAuthoring:
    cases, _ = load_family_case_modules(
        workspace,
        change_id=change_id,
        family=family,
        capability_leafs=capability_leafs,
        case_paths=case_paths,
    )
    return cases


def plan_review_input_paths(
    workspace: Path,
    *,
    change_id: str,
    family: Family,
) -> tuple[str, ...]:
    """Return the mechanically locked files a plan reviewer must read exactly."""
    _change_root(workspace, change_id)
    cases_root = workspace / "qa" / "cases"
    try:
        cases_root.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("reviewed case directory escapes the attempt workspace") from error
    if not cases_root.is_dir() or cases_root.is_symlink():
        raise InputError("reviewed case directory is missing: qa/cases")
    case_files = tuple(sorted(cases_root.glob("**/case.yaml"), key=lambda item: item.as_posix()))
    if not case_files:
        raise InputError("reviewed case files are missing: qa/cases/**/case.yaml")

    relative_paths = (
        f"qa/results/codegen/{family}-codegen-summary.md",
        f"qa/results/codegen/{family}-generated-files.json",
        "qa/proposal.md",
        *(path.relative_to(workspace).as_posix() for path in case_files),
    )
    for relative in relative_paths:
        if "/cases/" in relative:
            label = "case input"
        elif relative.endswith("/proposal.md"):
            label = "proposal input"
        else:
            label = "plan input"
        _regular_input_file(
            workspace,
            workspace.joinpath(*PurePosixPath(relative).parts),
            label=label,
        )
    return tuple(sorted(relative_paths))


def plan_repair_review(
    workspace: Path,
    *,
    business: PlanInputV1,
    family: Family,
) -> dict[str, object] | None:
    """Load the current review for a graph-authorized automatic or human-requested retry."""
    if business.local_round == 0:
        return None
    relative = f"qa/results/review/{family}-codegen-review.json"
    path = _regular_input_file(
        workspace,
        workspace.joinpath(*PurePosixPath(relative).parts),
        label="plan repair review",
    )
    try:
        data = path.read_bytes()
        raw = json.loads(data)
        review = PlanReviewAuthoring.model_validate(
            raw,
            context={"capability_leafs": leafs_of(business.capability_leafs)},
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise InputError(f"plan repair review is invalid: {relative}: {error}") from error
    if review.change_id != business.change_id:
        raise InputError("plan repair review change_id does not match the locked change_id")
    if review.review_type != f"{family}-codegen":
        raise InputError(f"codegen repair review_type does not match {family}-codegen")
    repair = {
        "review_path": relative,
        "review_digest": hashlib.sha256(data).hexdigest(),
        "plan_repair_review": review.model_dump(mode="json"),
    }
    if family == "api":
        repair["plan_repair_scope"] = {
            "allowed_artifacts": [
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
            ],
            "finding_ids": list(review.finding_ids),
            "related_consistency_edits": True,
            "preserve_case_scope_and_oracles": True,
            "mapping_changes_require_explicit_finding": True,
        }
    return repair


def constraints_for_cases(*, family: Family, change_id: str, cases: CaseYamlAuthoring) -> FamilyConstraintsV1:
    from assurance_generation.contracts.codegen import FAMILY_TARGET_ROOTS

    del change_id
    entries = tuple((*cases.added, *cases.modified))
    return FamilyConstraintsV1(
        write_roots=FAMILY_TARGET_ROOTS[family],
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
    selected_cases: CaseYamlAuthoring | None = None
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
        from assurance_generation.operations.selected_cases import load_selected_case_authoring

        selected_cases, _ = load_selected_case_authoring(
            workspace,
            reviewed,
            family=family,
            capability_leafs=business.capability_leafs,
        )
    if selected_cases is not None:
        cases = selected_cases
    elif business.reviewed_cases is None:
        cases = load_family_cases(
            workspace,
            change_id=business.change_id,
            family=family,
            capability_leafs=business.capability_leafs,
            case_paths=None,
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


def planning_facts_for(
    workspace: Path, *, change_id: str, family: Family, capability_leafs: tuple[str, ...]
) -> dict[str, Any]:
    targets: tuple[str, ...] = ()
    mapping_path = f"qa/results/plans/{family}-codegen-mapping.json"
    try:
        mapping = CodegenMapping.model_validate_json(_workspace_file(workspace, mapping_path).read_bytes())
        targets = tuple(entry.target_file for entry in mapping.entries)
    except (OSError, ValidationError):
        pass
    except OutputError as error:
        raise InputError(f"invalid planning index input: {error}") from error
    return build_planning_facts(
        workspace,
        change_id=change_id,
        capability_leafs=capability_leafs,
        families=(family,),
        target_files=targets,
    )


def review_input_images(workspace: Path, paths: tuple[str, ...]) -> dict[str, bytes]:
    return {
        path: _regular_input_file(workspace, workspace / path, label="review input").read_bytes()
        for path in paths
    }


def prepare_plan_request(
    *,
    family: Family,
    skill_path: str,
    business: PlanInputV1,
    cases: CaseYamlAuthoring,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
    close_result_capabilities: bool = False,
    review_input_paths: tuple[str, ...] = (),
    repair_review: Mapping[str, object] | None = None,
    extra_json: Mapping[str, object] | None = None,
    validation_error: str | None = None,
) -> AgentRunRequest:
    if business.family_constraints is None:
        raise InputError("family_constraints were not materialized")
    facts = planning_facts_for(
        context.project_root,
        change_id=business.change_id,
        capability_leafs=business.capability_leafs,
        family=family,
    )
    instructions = (
        InstructionPart.text("text/plain", resource_text(skill_path)),
        InstructionPart.from_json(cases.model_dump(mode="json")),
        InstructionPart.from_json(
            {
                **business.family_constraints.model_dump(mode="json"),
                "planning_facts": facts,
            }
        ),
    )
    if review_input_paths:
        instructions = (
            *instructions,
            InstructionPart.from_json({"review_input_paths": list(review_input_paths)}),
        )
    if business.reviewed_plan is not None:
        instructions = (
            *instructions,
            InstructionPart.from_json({"reviewed_plan": business.reviewed_plan}),
        )
    if repair_review is not None:
        instructions = (*instructions, InstructionPart.from_json(dict(repair_review)))
    if extra_json is not None:
        instructions = (*instructions, InstructionPart.from_json(dict(extra_json)))
    return agent_run_request(
        instructions=instructions,
        validation_error=validation_error,
        result=result_contract(
            result_schema_id,
            capability_leafs=business.capability_leafs if close_result_capabilities else None,
        ),
        binding=binding,
        roots=context,
        allowed_outputs=allowed_outputs,
        scope_id=business.change_id,
    )


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.result_payload)


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
    *,
    fallback_workspace: Path | None = None,
) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    for relative in declared:
        if not under_write_root(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            if fallback_workspace is None:
                raise OutputError(f"declared output file is missing: {relative}")
            path = _workspace_file(fallback_workspace, relative)
            if not path.is_file() or path.is_symlink():
                raise OutputError(f"declared output file is missing: {relative}")
        try:
            images[relative] = path.read_bytes()
        except OSError as error:
            raise OutputError(f"declared output file is unreadable: {relative}: {error}") from error
    return images


def _authenticate_codegen_mapping(
    *,
    document: PlanResultV1,
    family: Family,
    images: Mapping[str, bytes],
) -> CodegenMapping:
    relative = f"qa/results/plans/{family}-codegen-mapping.json"
    try:
        raw = json.loads(images[relative])
        mapping = CodegenMapping.model_validate(raw)
    except KeyError as error:
        raise OutputError(f"closed codegen mapping is missing: {relative}") from error
    except (UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise OutputError(f"closed codegen mapping is invalid: {relative}: {error}") from error
    if mapping.layer != family:
        raise OutputError(f"closed mapping layer {mapping.layer!r} does not match {family}")
    mapped_case_ids = tuple(sorted(item.case_id for item in mapping.entries))
    if mapped_case_ids != document.case_ids:
        raise OutputError(
            "closed mapping case IDs must exactly match the typed plan case_ids: "
            f"expected {list(document.case_ids)}, got {list(mapped_case_ids)}"
        )
    for entry in mapping.entries:
        if not family_allows_target(family, entry.target_file):
            raise OutputError(f"closed mapping target is outside {family} family policy: {entry.target_file}")
    return mapping


class PlanFinalizeHandler:
    input_model = AgentFinalizeInputV1

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
            if payload.change_id is not None and document.change_id != payload.change_id:
                raise OutputError("plan change_id does not match locked change_id")
            if tuple(sorted(document.output_files)) != plan_outputs(document.change_id, family):
                raise OutputError("output_files must declare the complete family plan package")
            if payload.reviewed_case is not None:
                try:
                    reviewed = authenticate_reviewed_case(
                        payload.reviewed_case,
                        context.project_root,
                        change_id=document.change_id,
                        coverage_epoch=payload.coverage_epoch,
                    )
                except ValueError as error:
                    raise InputError(str(error)) from error
                cases = load_family_cases(
                    context.project_root,
                    change_id=document.change_id,
                    family=family,
                    capability_leafs=payload.capability_leafs,
                    case_paths=tuple(item.path for item in reviewed.case_refs),
                )
                try:
                    document.require_case_scope(cases)
                except ValueError as error:
                    raise OutputError(str(error)) from error
            images = _authenticate_files(
                context.write_root,
                document.output_files,
                payload.artifact_paths,
                fallback_workspace=context.project_root if payload.local_round > 0 else None,
            )
            mapping = _authenticate_codegen_mapping(
                document=document,
                family=family,
                images=images,
            )
            facts = build_planning_facts(
                context.project_root,
                change_id=document.change_id,
                capability_leafs=payload.capability_leafs,
                families=(family,),
                target_files=tuple(entry.target_file for entry in mapping.entries),
            )
            contradictions = check_plan_consistency(images, mapping=mapping, facts=facts)
            if contradictions:
                raise OutputError("; ".join(contradictions))
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except (InputError, ValidationError) as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def planning_handler(family: str, kind: PrepareKind) -> TaskHandler:
    closed = closed_family(family)
    if kind == "finalize":
        return PlanFinalizeHandler(closed)
    raise ValueError(f"unknown planning handler kind: {kind}")


__all__ = [
    "FAMILIES",
    "PLAN_RESULT_ID",
    "PLAN_REVIEW_RESULT_ID",
    "Family",
    "PlanFinalizeHandler",
    "closed_family",
    "evidence_ref",
    "leafs_of",
    "load_family_case_modules",
    "planning_handler",
    "plan_review_input_paths",
    "authenticate_loop_round_history",
    "expected_plan_review_history",
    "prepare_plan_request",
    "request_family",
    "resolve_family",
    "result_contract",
    "validate_plan_input",
    "validate_reviewed_plan",
]
