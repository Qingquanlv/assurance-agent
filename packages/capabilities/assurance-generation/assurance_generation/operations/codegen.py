"""Capability-closed four-family codegen handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract, with_validation_retry
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    CodegenFinalizeInputV1,
    CodegenInputV1,
    FamilyConstraintsV1,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenGeneratedFileAuthoring,
    CodegenMapping,
    CodegenResultV1,
    CodegenScopeV1,
    durable_test_path,
)
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.obligation_methods import validate_observation_binding
from assurance_generation.contracts.plans import ObligationMethodPlanV1, canonical_relative_path
from assurance_generation.operations.codegen_scope import build_codegen_scope
from assurance_generation.operations.planning import (
    FAMILIES,
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
from assurance_intake.contracts.explore import PreparedExploreV1
from assurance_intake.operations.explore_context import load_exploration_document
from assurance_intake.contracts.obligations import PreparedObligationV1
from assurance_intake.operations.plan_codec import decode_plan
from assurance_intake.operations.obligations import normalize_obligation_drafts
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

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


def _authenticate_surface_ref(workspace: Path, ref: EvidenceArtifactRefV1) -> Path:
    path = workspace
    for part in PurePosixPath(ref.path).parts:
        path = path / part
        if path.is_symlink():
            raise InputError(f"surface document must not contain a symlink: {ref.path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(workspace.resolve())
    except (OSError, ValueError) as error:
        raise InputError(f"surface document is missing: {ref.path}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise InputError(f"surface document must be a regular single-link file: {ref.path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
        raise InputError(f"evidence digest changed after it was committed: {ref.path}")
    return path


def _verification_obligations(
    workspace: Path,
    *,
    plan_ref: object,
    family: Family,
    required: bool,
) -> tuple[PreparedObligationV1, ...]:
    model_dump = getattr(plan_ref, "model_dump", None)
    if callable(model_dump):
        plan_ref = model_dump(mode="json")
    ref = EvidenceArtifactRefV1.model_validate(plan_ref)
    plan_path = _workspace_path(workspace, ref.path)
    if not plan_path.is_file() and not required:
        return ()
    plan_bytes = _workspace_regular_file(workspace, ref.path).read_bytes()
    if hashlib.sha256(plan_bytes).hexdigest() != ref.digest:
        raise InputError("frozen plan digest changed")
    try:
        plan = decode_plan(plan_bytes, ref)
        obligations_ref = plan.quality_goal.obligations_ref
        obligations_bytes = _workspace_regular_file(workspace, obligations_ref.path).read_bytes()
        if hashlib.sha256(obligations_bytes).hexdigest() != obligations_ref.digest:
            raise InputError("frozen obligation digest changed")
        exploration = load_exploration_document(obligations_bytes)
        rows = (
            exploration.minimum_required_coverage
            if isinstance(exploration, PreparedExploreV1)
            else normalize_obligation_drafts(exploration.minimum_required_coverage, resolved_quotes={})
        )
    except ValueError as error:
        raise InputError(f"invalid frozen obligations: {error}") from error
    layers = {"api", "both"} if family == "api" else {"e2e", "both"} if family == "e2e" else set()
    return tuple(
        row
        for row in rows
        if row.scope_disposition == "included" and row.layer in layers and row.verification_requirements
    )


def _validate_method_plans(
    *,
    obligations: tuple[PreparedObligationV1, ...],
    methods: tuple[ObligationMethodPlanV1, ...],
    scope: CodegenScopeV1,
    mapping: CodegenMapping,
) -> None:
    expected = {}
    for obligation in obligations:
        if len(obligation.verification_requirements) != 1:
            raise InputError(
                f"obligation must have exactly one verification requirement in v1: {obligation.mrc_id}"
            )
        requirement = obligation.verification_requirements[0]
        expected[obligation.mrc_id] = requirement
    actual = {item.mrc_id: item for item in methods}
    if len(actual) != len(methods) or set(actual) != set(expected):
        raise OutputError("method_plans must exactly cover the family verification obligations")
    selectors = {f"{item.target_file}::{item.symbol.replace('.', '::')}" for item in mapping.entries}
    case_ids = set(scope.case_ids)
    for mrc_id, requirement in expected.items():
        method = actual[mrc_id]
        if (
            method.requirement_id != requirement.requirement_id
            or method.profile_id != requirement.profile_id
            or method.prerequisites != requirement.prerequisites
            or not method.case_ids
            or not set(method.case_ids) <= case_ids
        ):
            raise OutputError(f"method plan does not match frozen requirement: {mrc_id}")
        step_ids = {item.step_id for item in method.steps}
        if len(step_ids) != len(method.steps) or any(
            item.step_id not in step_ids or item.test_nodeid not in selectors for item in method.observations
        ):
            raise OutputError(f"method plan bindings are not executable: {mrc_id}")
        try:
            validate_observation_binding(requirement, method.observations)
        except ValueError as error:
            raise OutputError(f"invalid method observation binding for {mrc_id}: {error}") from error


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
    selected_scope: tuple[CaseYamlAuthoring, dict[str, tuple[str, ...]]] | None = None
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

        selected_scope = load_selected_case_authoring(
            workspace,
            reviewed,
            family=family,
            capability_leafs=business.capability_leafs,
        )
    if selected_scope is None:
        cases, case_ids_by_path = load_family_case_modules(
            workspace,
            change_id=business.change_id,
            family=family,
            capability_leafs=business.capability_leafs,
            case_paths=None,
        )
    else:
        cases, case_ids_by_path = selected_scope
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
    scope: CodegenScopeV1,
    cases: CaseYamlAuthoring,
    context_payload: Mapping[str, object],
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    scope_id: str,
    allowed_outputs: tuple[str, ...],
    validation_error: str | None = None,
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=with_validation_retry(
            (
                InstructionPart.text("text/plain", resource_text(skill_path)),
                InstructionPart.from_json(scope.model_dump(mode="json")),
                InstructionPart.from_json(cases.model_dump(mode="json")),
                InstructionPart.from_json({**context_payload, "allowed_outputs": list(allowed_outputs)}),
            ),
            validation_error,
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


def _workspace_path(workspace: Path, relative: str) -> Path:
    try:
        canonical_relative_path(relative)
    except ValueError as error:
        raise OutputError(str(error)) from error
    path = workspace
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise OutputError(f"declared output path contains a symlink: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    return path


def _workspace_regular_file(workspace: Path, relative: str) -> Path:
    path = _workspace_path(workspace, relative)
    if not path.is_file():
        raise OutputError(f"declared output file is missing: {relative}")
    if path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _baseline_files(
    previous: object,
    scope: CodegenScopeV1,
    capability_leafs: tuple[str, ...],
) -> dict[str, GeneratedFileEntryV1]:
    if previous is None:
        return {}
    try:
        baseline = CodegenResultV1.model_validate(
            previous, context={"capability_leafs": leafs_of(capability_leafs)}
        )
    except ValidationError as error:
        raise InputError(f"invalid codegen baseline: {error}") from error
    if baseline.change_id != scope.change_id or baseline.layer != scope.family:
        raise InputError("codegen baseline change_id and family must match the locked scope")
    paths = [entry.repo_path for entry in baseline.files]
    if len(paths) != len(set(paths)):
        raise InputError("codegen baseline contains duplicate file paths")
    # An earlier scope may include other modules; they confer no authority here.
    return {
        entry.repo_path: entry
        for entry in baseline.files
        if entry.repo_path.startswith("qa/tests/") and entry.repo_path in scope.locked_outputs
    }


def _seed_baseline(project: Path, staging: Path, baseline: Mapping[str, GeneratedFileEntryV1]) -> None:
    if not baseline:
        return
    if project.resolve() == staging.resolve():
        raise InputError("codegen repair requires a separate staging workspace")
    pending: list[tuple[Path, bytes]] = []
    try:
        for relative, entry in baseline.items():
            payload = _workspace_regular_file(project, relative).read_bytes()
            if _digest_bytes(payload) != entry.content_sha256:
                raise InputError(f"codegen baseline digest mismatch: {relative}")
            target = _workspace_path(staging, relative)
            if target.exists():
                # Prepare replay must not overwrite a repair already made in this attempt.
                _workspace_regular_file(staging, relative)
            else:
                pending.append((target, payload))
        # Verify all sources and destinations before materializing any baseline bytes.
        for target, payload in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(payload)
    except (OutputError, OSError) as error:
        raise InputError(f"cannot seed codegen baseline: {error}") from error


def _mapped_case_ids(mapping: CodegenMapping, path: str) -> tuple[str, ...]:
    return tuple(sorted(item.case_id for item in mapping.entries if item.target_file == path))


def _complete_files(
    workspace: Path,
    files: tuple[CodegenGeneratedFileAuthoring, ...],
    mapping: CodegenMapping,
    *,
    change_id: str,
    family: Family,
    allowed_paths: Collection[str],
    baseline: Mapping[str, GeneratedFileEntryV1] | None = None,
) -> tuple[GeneratedFileEntryV1, ...]:
    del change_id, family
    mapped_targets = {item.target_file for item in mapping.entries}
    if not files and mapped_targets:
        raise OutputError("empty files array is invalid when mapping targets exist")
    completed: list[GeneratedFileEntryV1] = []
    listed_test_entries: set[str] = set()
    for entry in files:
        target = entry.repo_path
        if target not in allowed_paths:
            raise OutputError(f"undeclared generated/modified test file: {target}")
        if entry.role == "test_entry" and target not in mapped_targets:
            raise OutputError(f"generated test file is absent from the closed mapping: {target}")
        try:
            staged = durable_test_path(target)
        except ValueError as error:
            raise OutputError(str(error)) from error
        payload = _workspace_regular_file(workspace, staged).read_bytes()
        digest = _digest_bytes(payload)
        if entry.disposition == "reused":
            prior = None if baseline is None else baseline.get(target)
            if prior is None or prior.content_sha256 != digest:
                raise OutputError(f"reused file must match the authenticated codegen baseline: {target}")
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
                content_sha256=digest,
            )
        )
    for target in sorted(mapped_targets):
        if target not in allowed_paths:
            raise OutputError(f"undeclared generated/modified test file: {target}")
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
            baseline = _baseline_files(business.codegen_output, scope, business.capability_leafs)
            _seed_baseline(context.project_root, context.write_root, baseline)
            context_payload: dict[str, object] = {
                "change_id": business.change_id,
                "family_constraints": business.family_constraints.model_dump(mode="json"),
                "generated_files_root": "qa/tests",
                "codegen_scope": scope.model_dump(mode="json"),
                "baseline_files": sorted(baseline),
                "verification_obligations": [
                    item.model_dump(mode="json")
                    for item in _verification_obligations(
                        context.project_root,
                        plan_ref=business.plan_ref,
                        family=family,
                        required=business.reviewed_case is not None,
                    )
                ],
            }
            # Lazy: quality.contracts.surface must not load at generation import time.
            if business.ui_exploration_ref is not None or business.api_discovery_ref is not None:
                from assurance_quality.contracts.surface import (
                    ApiDiscoveryDocument,
                    UiExplorationDocument,
                )

                if business.ui_exploration_ref is not None:
                    ui_path = _authenticate_surface_ref(context.project_root, business.ui_exploration_ref)
                    try:
                        ui_exploration = UiExplorationDocument.model_validate_json(ui_path.read_bytes())
                    except (OSError, ValidationError, ValueError) as error:
                        raise InputError(f"invalid ui-exploration.json: {error}") from error
                    if ui_exploration.change_id != business.change_id:
                        raise InputError("ui-exploration.json change_id does not match codegen change_id")
                    context_payload["ui_exploration"] = ui_exploration.model_dump(mode="json")
                if business.api_discovery_ref is not None:
                    api_path = _authenticate_surface_ref(context.project_root, business.api_discovery_ref)
                    try:
                        api_discovery = ApiDiscoveryDocument.model_validate_json(api_path.read_bytes())
                    except (OSError, ValidationError, ValueError) as error:
                        raise InputError(f"invalid api-discovery.json: {error}") from error
                    if api_discovery.change_id != business.change_id:
                        raise InputError("api-discovery.json change_id does not match codegen change_id")
                    context_payload["api_discovery"] = api_discovery.model_dump(mode="json")
            return prepare_codegen_outcome(
                skill_path=_SKILL_FILES[family],
                scope=scope,
                cases=cases,
                context_payload=context_payload,
                binding=binding,
                result_schema_id=CODEGEN_RESULT_ID,
                context=context,
                scope_id=business.change_id,
                allowed_outputs=codegen_outputs(scope),
                validation_error=business.validation_error,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CodegenFinalizeHandler:
    input_model = CodegenFinalizeInputV1

    def __init__(self, family: Family | None = None) -> None:
        self._family: Family | None = None if family is None else closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            family = resolve_family(self._family, request)
            payload = CodegenFinalizeInputV1.model_validate(request.input)
            document = _finalize_authoring(payload, family)
            _, scope, _ = validate_codegen_input(
                {
                    "change_id": payload.change_id or document.change_id,
                    "plan_digest": payload.plan_digest,
                    "plan_ref": payload.plan_ref.model_dump(mode="json"),
                    "capability_leafs": list(payload.capability_leafs),
                    "reviewed_case": None
                    if payload.reviewed_case is None
                    else payload.reviewed_case.model_dump(mode="json"),
                },
                family,
                context.project_root,
            )
            mapped_ids = tuple(sorted(item.case_id for item in document.mapping.entries))
            if mapped_ids != scope.case_ids:
                raise OutputError("mapping case IDs must exactly match the host codegen scope")
            locked_generated = {path for path in scope.locked_outputs if path.startswith("qa/tests/")}
            test_by_case = {
                case_id: row.test_file for row in scope.locked_modules for case_id in row.case_ids
            }
            receipt_tests = {entry.repo_path for entry in document.files}
            if receipt_tests != locked_generated:
                raise OutputError(
                    "codegen receipt test paths do not match locked outputs; "
                    f"missing={sorted(locked_generated - receipt_tests)}, "
                    f"unexpected={sorted(receipt_tests - locked_generated)}"
                )
            for item in document.mapping.entries:
                expected = test_by_case.get(item.case_id)
                if expected is None or item.target_file != expected:
                    raise OutputError(
                        f"mapping target_file must equal the locked test file for {item.case_id}"
                    )
            _validate_method_plans(
                obligations=_verification_obligations(
                    context.project_root,
                    plan_ref=payload.plan_ref,
                    family=family,
                    required=payload.reviewed_case is not None,
                ),
                methods=document.method_plans,
                scope=scope,
                mapping=document.mapping,
            )
            files = _complete_files(
                context.write_root,
                document.files,
                document.mapping,
                change_id=document.change_id,
                family=family,
                allowed_paths=locked_generated,
                baseline=_baseline_files(payload.codegen_output, scope, payload.capability_leafs),
            )
            result = CodegenResultV1.model_validate(
                {
                    "schema_version": "1",
                    "change_id": document.change_id,
                    "layer": document.layer,
                    "files": [item.model_dump(mode="json") for item in files],
                    "mapping": document.mapping.model_dump(mode="json"),
                    "required_capabilities": list(document.required_capabilities),
                    "method_plans": [item.model_dump(mode="json") for item in document.method_plans],
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
