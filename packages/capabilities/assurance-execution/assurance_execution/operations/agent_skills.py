"""Provider-neutral execute/run prepare and finalize handlers."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError
import yaml

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.attempts import AttemptKey
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import (
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_execution.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    ExecutionPrepareInputV1,
    ExecuteInputV1,
    RunSkillInputV1,
    SelectInputV1,
)
from assurance_execution.contracts.evidence import ExecutionAgentResultV1, ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_execution.contracts.verification import (
    VerificationManifestV1,
)
from assurance_execution.execution_view import (
    ExecutionView,
    build_or_authenticate_execution_view,
    collect_test_support_files,
    discard_authenticated_execution_view,
    execution_view_relative,
)
from assurance_execution.generated_merge import merge_generated
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
from assurance_execution.operations.paths import resolve_canonical_evidence
from assurance_execution.operations.runner import write_canonical_evidence
from assurance_execution.operations.selection import close_mappings
from assurance_execution.operations.managed_sut import authenticate_managed_sut_receipts
from assurance_execution.operations.sqlite_oracle import observe_user
from assurance_execution.operations.verification_manifest import (
    authenticate_verification_manifest,
    build_verification_manifest,
)
from assurance_generation.contracts.admission import admit_verified_generation
from assurance_generation.contracts import CaseExecutionPlanSetV1, CodegenAuthoringV1
from assurance_intake.contracts import CaseYamlAuthoring
from assurance_intake.contracts.plan import decode_plan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
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
        "receipt": "ordered-family-command-receipts.v1",
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
_BASELINE_POLICY_ID = "assurance.execution.closed-baseline.v1"
_BASELINE_SOURCE_ROOTS = ("app", "migrations", "src", "web/build", "web/src")
_BASELINE_CONFIG_FILES = (
    ".python-version",
    "Pipfile",
    "Pipfile.lock",
    "bun.lock",
    "bun.lockb",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "pdm.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "poetry.lock",
    "pyproject.toml",
    "pytest.ini",
    "requirements-dev.txt",
    "requirements.txt",
    "ruff.toml",
    "setup.cfg",
    "tox.ini",
    "uv.lock",
    "yarn.lock",
    "web/.env",
    "web/index.html",
    "web/bun.lock",
    "web/bun.lockb",
    "web/jsconfig.json",
    "web/npm-shrinkwrap.json",
    "web/package-lock.json",
    "web/package.json",
    "web/playwright.config.js",
    "web/playwright.config.ts",
    "web/pnpm-lock.yaml",
    "web/tsconfig.json",
    "web/unocss.config.js",
    "web/vite.config.js",
    "web/vite.config.ts",
    "web/vitest.config.js",
    "web/vitest.config.ts",
    "web/yarn.lock",
)
_BASELINE_IGNORED_DIRECTORIES = frozenset(
    {
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "logs",
        "node_modules",
        "venv",
    }
)
_MAX_BASELINE_FILES = 10_000
_MAX_BASELINE_FILE_BYTES = 16 * 1024 * 1024
_MAX_BASELINE_BYTES = 256 * 1024 * 1024


@dataclass
class _BaselineManifest:
    entries: dict[str, tuple[str, int]] = field(default_factory=dict)
    total_bytes: int = 0

    def add(self, relative: str, payload: bytes) -> None:
        if len(payload) > _MAX_BASELINE_FILE_BYTES:
            raise InputError(f"workspace baseline file exceeds size limit: {relative}")
        previous = self.entries.get(relative)
        if previous is None and len(self.entries) >= _MAX_BASELINE_FILES:
            raise InputError("workspace baseline exceeds file count limit")
        next_total = self.total_bytes - (0 if previous is None else previous[1]) + len(payload)
        if next_total > _MAX_BASELINE_BYTES:
            raise InputError("workspace baseline exceeds total size limit")
        self.entries[relative] = (hashlib.sha256(payload).hexdigest(), len(payload))
        self.total_bytes = next_total

    def members(self) -> list[list[str]]:
        return [[path, self.entries[path][0]] for path in sorted(self.entries)]


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
    read_roots: tuple[str, ...],
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
        "read_roots": tuple(sorted(set(read_roots))),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def prepare_outcome(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
    read_roots: tuple[str, ...],
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
            read_roots=read_roots,
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


def authenticate_generation_result(root: ExecutionPrepareInputV1, workspace: Path) -> None:
    """Authenticate the accepted generation semantics before verified host dispatch."""

    generation = root.generation_result
    if generation is None:
        if root.validation_profile is not None:
            raise InputError("verified execution requires the accepted generation result")
        return
    if generation.change_id != root.change_id or generation.coverage_epoch != root.coverage_epoch:
        raise InputError("generation result identity does not match execution input")
    if generation.plan_digest != root.plan_digest or generation.plan_ref != root.plan_ref:
        raise InputError("generation plan binding does not match execution input")
    if root.validation_profile is None:
        for ref in (*generation.source_refs, generation.mapping_ref):
            path = _regular_input_file(workspace, ref.path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
                raise InputError(f"execution source digest changed: {ref.path}")
        return
    profile = root.verification
    machine_ref = generation.case_execution_plan_ref
    if (profile is not None and profile.validation_profile != root.validation_profile) or machine_ref is None:
        raise InputError("verified execution profile is incomplete")
    try:
        admission = admit_verified_generation(
            workspace,
            workspace,
            change_id=root.change_id,
            coverage_epoch=root.coverage_epoch,
            plan_digest=root.plan_digest,
            plan_ref=root.plan_ref,
            reviewed_case=generation.reviewed_case,
            validation_profile=root.validation_profile,
            selected_test_families=root.selected_test_families,
            capability_leafs=root.capability_leafs,
            case_execution_plan_ref=machine_ref,
        )
    except ValueError as error:
        raise InputError(f"accepted generation is not semantically admitted: {error}") from error
    expected_mapping_path = (
        f"qa/changes/{root.change_id}/generation/epochs/{root.coverage_epoch}/mapping.json"
    )
    expected_mapping_bytes = (
        canonical_json_bytes(cast(JSONValue, admission.closed_mapping.model_dump(mode="json"))) + b"\n"
    )
    mapping_path = _regular_input_file(workspace, generation.mapping_ref.path)
    if (
        generation.reviewed_case != admission.reviewed_case
        or (profile is not None and generation.case_execution_plan_ref != profile.case_execution_plan_ref)
        or generation.case_execution_plan_digest != machine_ref.digest
        or generation.source_refs != admission.source_refs
        or generation.plan_refs != admission.plan_refs
        or generation.mapping_ref.path != expected_mapping_path
        or hashlib.sha256(mapping_path.read_bytes()).hexdigest() != generation.mapping_ref.digest
        or mapping_path.read_bytes() != expected_mapping_bytes
    ):
        raise InputError("accepted generation result differs from semantic admission")
    if profile is None:
        return
    mapped_case = next(
        (item.case_id for item in admission.closed_mapping.mappings if item.test == profile.nodeid), None
    )
    if mapped_case is None or mapped_case not in {item.case_id for item in admission.machine_plans.cases}:
        raise InputError("verified execution nodeid is outside the accepted generation mapping")


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


def _workspace_tree_id(
    workspace: Path,
    *,
    excluded_root: Path,
    merged_generated_digest: str,
) -> str:
    manifest = _BaselineManifest()
    try:
        excluded = excluded_root.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError("attempt write root must remain inside the project") from error
    for relative_root in _BASELINE_SOURCE_ROOTS:
        _add_baseline_tree(workspace, relative_root, excluded=excluded, manifest=manifest)
    try:
        support_files = collect_test_support_files(workspace)
    except ValueError as error:
        raise InputError(str(error)) from error
    for relative, source in support_files.items():
        payload, _ = source
        manifest.add(relative, payload)
    for relative in _baseline_config_members(workspace):
        _add_baseline_path(workspace, relative, manifest=manifest)
    return canonical_digest(
        {
            "policy_id": _BASELINE_POLICY_ID,
            "members": manifest.members(),
            "merged_generated_digest": merged_generated_digest,
        }
    )


def _add_baseline_tree(
    workspace: Path,
    relative_root: str,
    *,
    excluded: Path,
    manifest: _BaselineManifest,
) -> None:
    root = workspace.joinpath(*PurePosixPath(relative_root).parts)
    if root.is_symlink():
        raise InputError(f"workspace baseline contains a symbolic link: {relative_root}")
    if not root.exists():
        return
    if not root.is_dir():
        raise InputError(f"workspace baseline source root is not a directory: {relative_root}")
    try:
        root.resolve().relative_to(workspace.resolve())
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for name in sorted(directories):
                child = current_path / name
                relative = child.relative_to(workspace)
                if relative == excluded or name in _BASELINE_IGNORED_DIRECTORIES:
                    continue
                if child.is_symlink():
                    raise InputError(f"workspace baseline contains a symbolic link: {relative.as_posix()}")
                kept.append(name)
            directories[:] = kept
            for name in sorted(filenames):
                if _runtime_noise_file(name):
                    continue
                relative = (current_path / name).relative_to(workspace)
                if relative == excluded or excluded in relative.parents:
                    continue
                _add_baseline_path(workspace, relative.as_posix(), manifest=manifest)
    except InputError:
        raise
    except ValueError as error:
        raise InputError(f"workspace baseline source root escapes project: {relative_root}") from error
    except OSError as error:
        raise InputError(f"could not scan workspace baseline root: {relative_root}") from error


def _baseline_config_members(workspace: Path) -> tuple[str, ...]:
    members: set[str] = set(_BASELINE_CONFIG_FILES)
    for relative_root in (".", "web"):
        root = workspace if relative_root == "." else workspace / relative_root
        if root.is_symlink():
            raise InputError(f"workspace baseline contains a symbolic link: {relative_root}")
        if not root.exists():
            continue
        if not root.is_dir():
            raise InputError(f"workspace baseline config root is not a directory: {relative_root}")
        try:
            for child in root.iterdir():
                package_manifest = child.name.startswith("package") and child.name.endswith(".json")
                web_runtime_config = relative_root == "web" and (
                    child.name.startswith(".env") or child.name == "index.html" or ".config." in child.name
                )
                if package_manifest or web_runtime_config:
                    members.add(child.relative_to(workspace).as_posix())
        except OSError as error:
            raise InputError(f"could not scan workspace baseline config root: {relative_root}") from error
    return tuple(sorted(members))


def _add_baseline_path(workspace: Path, relative: str, *, manifest: _BaselineManifest) -> None:
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise InputError(f"workspace baseline contains a symbolic link: {relative}")
    if not path.exists():
        return
    try:
        path.resolve().relative_to(workspace.resolve())
        stat = path.stat()
        if not path.is_file() or stat.st_nlink != 1:
            raise InputError(f"workspace baseline contains a non-regular file: {relative}")
        if stat.st_size > _MAX_BASELINE_FILE_BYTES:
            raise InputError(f"workspace baseline file exceeds size limit: {relative}")
        payload = path.read_bytes()
    except InputError:
        raise
    except (OSError, ValueError) as error:
        raise InputError(f"could not authenticate workspace baseline file: {relative}") from error
    manifest.add(relative, payload)


def _runtime_noise_file(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(
        (".db", ".sqlite", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")
    ) or lowered.endswith((".log", ".pyc", ".pyo"))


def assemble_execution_input(
    data: object,
    *,
    workspace: Path,
    write_root: Path,
    model: type[ExecuteInputV1] | type[RunSkillInputV1],
    request: TaskRequest | None = None,
    context: TaskContext | None = None,
) -> ExecuteInputV1 | RunSkillInputV1:
    root = validate_input(ExecutionPrepareInputV1, data)
    try:
        plan = decode_plan(
            _regular_input_file(workspace, root.plan_ref.path).read_bytes(),
            root.plan_ref,
        )
    except ValueError as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.plan_digest != root.plan_digest:
        raise InputError("execution plan digest does not match frozen plan")
    if plan.selected_test_families != root.selected_test_families:
        raise InputError("execution families differ from frozen plan")
    authenticate_generation_result(root, workspace)
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
    try:
        merged = merge_generated(workspace, root.change_id, root.selected_test_families)
        locked_baseline = _workspace_tree_id(
            workspace,
            excluded_root=write_root,
            merged_generated_digest=merged.digest,
        )
        runner_profile_digest = _RUNNER_PROFILE_DIGEST
        batch_id = canonical_digest(
            {
                "change_id": root.change_id,
                "mapping": closed.model_dump(mode="json"),
                "baseline_tree_id": locked_baseline,
                "runner_profile_digest": runner_profile_digest,
            }
        )
        verified = _prepare_verified_execution(
            root,
            workspace=workspace,
            write_root=write_root,
            closed=closed,
            request=request,
            context=context,
        )
        if verified is None:
            view = build_or_authenticate_execution_view(
                workspace,
                write_root=write_root,
                change_id=root.change_id,
                batch_id=batch_id,
                merged=merged,
                selected=closed.selected,
            )
        else:
            manifest, _ = verified
            view = build_or_authenticate_execution_view(
                workspace,
                write_root=write_root,
                request=merged.execution_view_input(
                    change_id=root.change_id,
                    batch_id=batch_id,
                    execution_id=manifest.execution_id,
                    selected=closed.selected,
                ),
            )
        physical_view = write_root.joinpath(*PurePosixPath(view.root).parts)
        execution_view_root = physical_view.resolve().relative_to(workspace.resolve()).as_posix()
        if view.executed_at is None:
            raise ValueError("execution view preparation time is missing")
    except ValueError as error:
        raise InputError(str(error)) from error
    return model(
        change_id=root.change_id,
        plan_digest=root.plan_digest,
        plan_ref=root.plan_ref,
        batch_id=batch_id,
        capability_leafs=root.capability_leafs,
        case_ids=case_ids,
        artifact_paths=(),
        mapping=closed,
        selected_targets=selected,
        baseline_tree_id=locked_baseline,
        runner_profile_digest=runner_profile_digest,
        coverage_epoch=root.coverage_epoch,
        repair_round=root.repair_round,
        generation_result=root.generation_result,
        execution_view_root=execution_view_root,
        execution_view_digest=view.digest,
        executed_at=view.executed_at,
        execution_id=None if verified is None else verified[0].execution_id,
        verification_manifest_ref=None if verified is None else verified[1],
    )


def _prepare_verified_execution(
    root: ExecutionPrepareInputV1,
    *,
    workspace: Path,
    write_root: Path,
    closed: ClosedMappingV1,
    request: TaskRequest | None,
    context: TaskContext | None,
) -> tuple[VerificationManifestV1, EvidenceArtifactRefV1] | None:
    profile = root.verification
    if profile is None:
        return None
    if request is None or context is None:
        raise InputError("verified execution requires authenticated task context")
    try:
        attempt_key = AttemptKey(digest=context.workspace_identity.task_id)
    except ValidationError as error:
        raise InputError("verified execution workspace does not carry an AttemptKey") from error
    if profile.nodeid not in closed.selected:
        raise InputError("verified execution nodeid is outside the closed mapping")
    plan_path = _regular_input_file(workspace, profile.case_execution_plan_ref.path)
    plan_bytes = plan_path.read_bytes()
    if hashlib.sha256(plan_bytes).hexdigest() != profile.case_execution_plan_ref.digest:
        raise InputError("case execution plan digest changed before verified prepare")
    try:
        plan_set = CaseExecutionPlanSetV1.model_validate_json(plan_bytes)
    except ValidationError as error:
        raise InputError(f"case execution plan is invalid: {error}") from error
    if len(plan_set.cases) != 1:
        raise InputError("verified execution requires exactly one case execution plan")
    plan = plan_set.cases[0]
    mapped_case = next(item.case_id for item in closed.mappings if item.test == profile.nodeid)
    if (
        plan_set.change_id != root.change_id
        or plan.case_id != mapped_case
        or plan.plan_ref != root.plan_ref
        or plan.plan_digest != root.plan_digest
        or plan.coverage_epoch != root.coverage_epoch
        or plan.validation_profile != profile.validation_profile
    ):
        raise InputError("verified execution profile does not match the frozen machine plan")
    generation = root.generation_result
    if generation is None:
        raise InputError("verified execution requires the accepted generation result")
    mapping_path = _regular_input_file(workspace, generation.mapping_ref.path)
    mapping_bytes = mapping_path.read_bytes()
    if hashlib.sha256(mapping_bytes).hexdigest() != generation.mapping_ref.digest:
        raise InputError("generation mapping digest changed before verified prepare")
    authorization_digest = context.workspace_identity.identity_digest
    activity_digest = canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "invocation_id": request.invocation_id,
            "task_id": request.task_id,
            "graph_instance_id": request.graph_instance_id,
            "node_id": request.node_id,
            "workspace_identity_digest": context.workspace_identity.identity_digest,
        }
    )
    managed_path, observer_path, database_identity, authority_digest = authenticate_managed_sut_receipts(
        workspace,
        profile,
        secret_port=context.secrets,
        authorization_scope_digest=authorization_digest,
        activity_receipt_digest=activity_digest,
    )
    execution_id = _execution_id(attempt_key, profile.nodeid)
    _safe_staging_component(root.change_id, "change_id")
    manifest = build_verification_manifest(
        change_id=root.change_id,
        case_id=plan.case_id,
        nodeid=profile.nodeid,
        invocation_id=request.invocation_id,
        task_id=request.task_id,
        graph_instance_id=request.graph_instance_id,
        attempt_key=attempt_key,
        business_activation=profile.business_activation,
        coverage_epoch=root.coverage_epoch,
        repair_round=root.repair_round,
        authorization_scope_digest=authorization_digest,
        activity_receipt_digest=activity_digest,
        plan_ref=root.plan_ref.path,
        plan_digest=root.plan_digest,
        case_execution_plan_ref=profile.case_execution_plan_ref.path,
        case_execution_plan_digest=profile.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        mapping_digest=generation.mapping_ref.digest,
        sut_digest=plan.sut_digest,
        technical_config_digest=plan.technical_config_digest,
        validation_profile=profile.validation_profile,
        sut_base_url=profile.sut_base_url,
        sut_instance_id=profile.sut_instance_id,
        sut_sqlite_path=managed_path,
        sqlite_path=observer_path,
        username=profile.user_inputs.username,
        email=profile.user_inputs.email,
        evidence_root=f"qa/changes/{root.change_id}/execution",
        execution_id=execution_id,
    )
    manifest_bytes = (json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    verification_root = (
        write_root
        / "qa"
        / "changes"
        / root.change_id
        / ".staging"
        / "execution"
        / "_verification"
        / attempt_key.digest
    )
    manifest_path = verification_root / "verification-manifest.json"
    receipt_path = verification_root / "verification-prepare-receipt.json"
    manifest_ref_path = manifest_path.resolve(strict=False).relative_to(workspace.resolve()).as_posix()
    receipt_document = {
        "schema_version": "1",
        "attempt_key": attempt_key.digest,
        "activity_receipt_digest": activity_digest,
        "manifest_path": manifest_ref_path,
        "manifest_sha256": manifest_digest,
        "inputs": profile.user_inputs.model_dump(mode="json"),
        "managed_sut_prepare_receipt_ref": profile.managed_sut_prepare_receipt_ref.model_dump(mode="json"),
        "managed_sut_start_receipt_ref": profile.managed_sut_start_receipt_ref.model_dump(mode="json"),
        "managed_sut_authority_handle": profile.managed_sut_authority_handle,
        "managed_sut_authority_digest": authority_digest,
    }
    receipt_bytes = (json.dumps(receipt_document, indent=2, sort_keys=True) + "\n").encode()
    if manifest_path.exists():
        try:
            recovered_manifest_bytes = _read_frozen_prepare_file(manifest_path, "verification manifest")
            recovered_receipt_bytes = _read_frozen_prepare_file(receipt_path, "independent prepare receipt")
            if recovered_receipt_bytes != receipt_bytes or recovered_manifest_bytes != manifest_bytes:
                raise ValueError("independent prepare receipt does not authenticate the manifest")
            recovered_manifest = VerificationManifestV1.model_validate_json(recovered_manifest_bytes)
            authenticate_verification_manifest(
                recovered_manifest,
                manifest_digest=canonical_digest(manifest.model_dump(mode="json")),
                attempt_key=attempt_key,
                invocation_id=request.invocation_id,
                nodeid=profile.nodeid,
                sqlite_path=observer_path,
                username=profile.user_inputs.username,
                email=profile.user_inputs.email,
                authorization_scope_digest=authorization_digest,
                activity_receipt_digest=activity_digest,
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"verification manifest recovery failed: {error}") from error
    else:
        try:
            observation = observe_user(
                observer_path,
                profile.user_inputs.username,
                profile.user_inputs.email,
                expected_identity=database_identity,
            )
            if observation["state"] != "observed":
                raise ValueError(f"SQLite input observation failed: {observation['reason']}")
            if observation["rows"]:
                raise ValueError("frozen User inputs already exist in managed SQLite")
            verification_root.mkdir(parents=True, exist_ok=False)
            _write_frozen_prepare_file(manifest_path, manifest_bytes)
            _write_frozen_prepare_file(receipt_path, receipt_bytes)
        except (OSError, ValueError) as error:
            raise InputError(f"could not freeze verified execution manifest: {error}") from error
    return manifest, EvidenceArtifactRefV1(path=manifest_ref_path, digest=manifest_digest)


def _safe_staging_component(value: str, label: str) -> None:
    if value in {"", ".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise InputError(f"verified execution {label} is not a safe path component")


def _execution_id(attempt_key: AttemptKey, nodeid: str) -> str:
    raw = bytearray(hashlib.sha256(f"{attempt_key.digest}\0{nodeid}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _write_frozen_prepare_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o400)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_frozen_prepare_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing")
    details = path.stat()
    if details.st_nlink != 1 or details.st_mode & 0o777 != 0o400:
        raise ValueError(f"{label} is not a frozen single-link file")
    return path.read_bytes()


def _finalize_payload(
    data: object,
) -> AgentFinalizeInputV1:
    return validate_input(AgentFinalizeInputV1, data)


def _discard_execution_view(
    payload: AgentFinalizeInputV1,
    *,
    project_root: Path,
    write_root: Path,
) -> None:
    relative = execution_view_relative(payload.change_id, payload.batch_id, payload.execution_id)
    expected = write_root.joinpath(*PurePosixPath(relative).parts)
    try:
        project_relative = expected.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as error:
        raise OutputError("execution view escapes the authenticated attempt workspace") from error
    if payload.execution_view_root != project_relative:
        raise OutputError("execution view root does not match the authenticated attempt workspace")
    view = ExecutionView(
        batch_id=payload.batch_id,
        root=relative,
        selected_targets=payload.mapping.selected,
        digest=payload.execution_view_digest,
        mode="verified" if payload.execution_id is not None else "legacy",
        execution_id=payload.execution_id,
    )
    try:
        discard_authenticated_execution_view(write_root, view)
    except ValueError as error:
        raise OutputError(str(error)) from error


def _commit_execution_evidence(
    payload: AgentFinalizeInputV1,
    evidence: ExecutionEvidenceV1,
    *,
    project_root: Path,
    write_root: Path,
    filename: str,
) -> None:
    output = resolve_canonical_evidence(write_root, evidence.change_id, filename)
    expected = json.dumps(evidence.model_dump(mode="json"), indent=2).encode("utf-8") + b"\n"
    view = write_root.joinpath(
        *PurePosixPath(
            execution_view_relative(payload.change_id, payload.batch_id, payload.execution_id)
        ).parts
    )
    if output.exists():
        if (
            output.is_symlink()
            or not output.is_file()
            or output.stat().st_nlink != 1
            or output.read_bytes() != expected
        ):
            raise OutputError("prepared execution evidence drifted before finalize recovery")
    else:
        if not view.is_dir() or view.is_symlink():
            raise OutputError("execution view is missing before first finalize")
        write_canonical_evidence(write_root, evidence, filename=filename)
    if view.exists():
        _discard_execution_view(
            payload,
            project_root=project_root,
            write_root=write_root,
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
        agent_result = ExecutionAgentResultV1.model_validate(
            _structured(payload),
            context={"capability_leafs": leafs, "case_ids": case_ids},
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if agent_result.mapping != locked:
        raise OutputError("execution evidence mapping does not match the locked closed mapping")
    if agent_result.change_id != payload.change_id or agent_result.batch_id != payload.batch_id:
        raise OutputError("execution evidence identity does not match the locked change")
    if agent_result.baseline_tree_id != payload.baseline_tree_id:
        raise OutputError("execution evidence baseline tree does not match the authenticated workspace")
    if agent_result.selected_targets != payload.selected_targets:
        raise OutputError("execution evidence targets do not match the locked selection")
    if agent_result.runner_profile_digest != payload.runner_profile_digest:
        raise OutputError("execution evidence runner profile does not match the locked selection")
    if payload.artifact_paths:
        _authenticate_files(workspace, payload.artifact_paths)
    result_failed = any(item.status == "failed" for item in agent_result.results)
    status = "failed" if agent_result.receipt.exit_code != 0 or result_failed else "passed"
    return ExecutionEvidenceV1.model_validate(
        {
            **agent_result.model_dump(mode="json"),
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_ref.model_dump(mode="json"),
            "executed_at": payload.executed_at,
            "status": status,
            "mapping_digest": mapping_digest(locked),
            "receipt_digest": json_digest(cast(JSONValue, agent_result.receipt.model_dump(mode="json"))),
        },
        context={"capability_leafs": leafs, "case_ids": case_ids},
    )


class ExecutePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            binding = validate_binding(request.binding_data)
            business = assemble_execution_input(
                request.input,
                workspace=context.project_root,
                write_root=context.write_root,
                model=ExecuteInputV1,
                request=request,
                context=context,
            )
            return prepare_outcome(
                skill_path=EXECUTE_SKILL,
                business=business,
                binding=binding,
                context=context,
                allowed_outputs=(),
                read_roots=(business.execution_view_root,),
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
                write_root=context.write_root,
                model=RunSkillInputV1,
                request=request,
                context=context,
            )
            return prepare_outcome(
                skill_path=RUN_SKILL,
                business=business,
                binding=binding,
                context=context,
                allowed_outputs=(),
                read_roots=(business.execution_view_root,),
            )
        except InputError as error:
            return failed_input(error)


class ExecuteFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = _finalize_payload(request.input)
            evidence = _finalize_evidence(payload, context.project_root)
            _commit_execution_evidence(
                payload,
                evidence,
                project_root=context.project_root,
                write_root=context.write_root,
                filename="execute-result.json",
            )
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RunFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = _finalize_payload(request.input)
            evidence = _finalize_evidence(payload, context.project_root)
            _commit_execution_evidence(
                payload,
                evidence,
                project_root=context.project_root,
                write_root=context.write_root,
                filename="run-result.json",
            )
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
