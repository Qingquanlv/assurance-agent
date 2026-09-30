"""Deterministic execution input and evidence helpers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError
import yaml

from agent_runtime_contracts.ops import InputError, OutputError, validate_model
from agent_runtime_contracts.qa_paths import qa_join
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json

from assurance_execution.contracts.agent import (
    AgentFinalizeInputV1,
    ExecutionPrepareInputV1,
    RunTestsInputV1,
    SelectInputV1,
)
from assurance_execution.contracts.evidence import ExecutionAgentResultV1, ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_execution.execution_view import (
    ExecutionView,
    authenticate_durable_execution,
    collect_test_support_files,
    lock_durable_execution,
)
from assurance_execution.generated_merge import merge_generated
from assurance_execution.operations.common import json_digest, leafs_of, mapping_digest
from assurance_execution.operations.paths import resolve_canonical_evidence
from assurance_execution.operations.runner import write_canonical_evidence
from assurance_execution.operations.selection import close_mappings
from assurance_generation.contracts import CodegenAuthoringV1
from assurance_intake.contracts import CaseYamlAuthoring
from assurance_intake.contracts.case_selection import CaseSelectionV1, selection_path
from assurance_intake.contracts.cases import CaseEntryAuthoring
from assurance_intake.operations.plan_codec import decode_plan
from assurance_intake.contracts.workflow import ReviewedCaseV1

_RUNNER_PROFILE_DIGEST = canonical_digest(
    {
        "profile": "assurance.execution.agent.v1",
        "selection": "closed-mapping-test-selector.v1",
        "evidence": "assurance.execution.result.execution.v1",
        "receipt": "ordered-family-command-receipts.v1",
    }
)
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


def _authenticate_generation_sources(root: ExecutionPrepareInputV1, workspace: Path) -> None:
    generation = root.generation_result
    if generation is None:
        return
    if generation.change_id != root.change_id or generation.coverage_epoch != root.coverage_epoch:
        raise InputError("generation result identity does not match execution input")
    if generation.plan_digest != root.plan_digest or generation.plan_ref != root.plan_ref:
        raise InputError("generation plan binding does not match execution input")
    for ref in (generation.mapping_ref, *generation.source_refs):
        path = _regular_input_file(workspace, ref.path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
            raise InputError(f"execution source digest changed: {ref.path}")


def _reviewed_cases(
    workspace: Path,
    *,
    capability_leafs: tuple[str, ...],
    reviewed_case: ReviewedCaseV1 | None = None,
) -> CaseYamlAuthoring:
    if reviewed_case is not None:
        selection_file = _regular_input_file(workspace, reviewed_case.selection_ref.path)
        if hashlib.sha256(selection_file.read_bytes()).hexdigest() != reviewed_case.selection_ref.digest:
            raise InputError("reviewed case selection digest changed")
        if reviewed_case.selection_ref.path != selection_path(reviewed_case.coverage_epoch):
            raise InputError("reviewed case selection does not bind the current epoch")
        try:
            selection = CaseSelectionV1.model_validate(_json_document(selection_file))
        except ValidationError as error:
            raise InputError(f"invalid reviewed case selection: {error}") from error
        if (
            selection.change_id != reviewed_case.change_id
            or selection.coverage_epoch != reviewed_case.coverage_epoch
            or selection.plan_digest != reviewed_case.plan_digest
        ):
            raise InputError("reviewed case selection identity does not match")
        review_file = _regular_input_file(workspace, reviewed_case.review_ref.path)
        if hashlib.sha256(review_file.read_bytes()).hexdigest() != reviewed_case.review_ref.digest:
            raise InputError("reviewed case review digest changed")
        case_refs = {item.path: item for item in reviewed_case.case_refs}
        selected: list[CaseEntryAuthoring] = []
        for row in selection.cases:
            source_ref = case_refs.get(row.source_ref.path)
            if source_ref is None or source_ref != row.source_ref:
                raise InputError("selected case source is not authenticated by the reviewed case")
            source_file = _regular_input_file(workspace, source_ref.path)
            if hashlib.sha256(source_file.read_bytes()).hexdigest() != source_ref.digest:
                raise InputError(f"selected case source digest changed: {source_ref.path}")
            try:
                document = yaml.safe_load(source_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                raise InputError(f"selected case source is invalid: {source_ref.path}") from error
            section, marker, raw_index = row.source_locator.partition("[")
            entries = document.get(section) if isinstance(document, dict) else None
            if not marker or not raw_index.endswith("]") or not raw_index[:-1].isdigit():
                raise InputError(f"selected case locator is invalid: {row.source_locator}")
            index = int(raw_index[:-1])
            if not isinstance(entries, list) or index >= len(entries):
                raise InputError(f"selected case locator is out of range: {row.source_locator}")
            try:
                entry = CaseEntryAuthoring.model_validate(entries[index])
            except ValidationError as error:
                raise InputError(f"selected case is invalid: {error}") from error
            if entry.case_id != row.case_id:
                raise InputError("selected case locator does not match case_id")
            selected.append(entry)
        try:
            return CaseYamlAuthoring.model_validate(
                {
                    "schema_version": "1.0",
                    "added": [entry.model_dump(mode="json") for entry in selected],
                    "modified": [],
                    "removed": [],
                },
                context={"capability_leafs": leafs_of(capability_leafs)},
            )
        except ValidationError as error:
            raise InputError(str(error)) from error
    root = workspace / "qa" / "cases"
    if not root.is_dir() or root.is_symlink():
        raise InputError("reviewed case directory is missing: qa/cases")
    paths = tuple(sorted(root.glob("**/case.yaml"), key=lambda item: item.as_posix()))
    if not paths:
        raise InputError("reviewed case files are missing: qa/cases/**/case.yaml")
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
    except ValueError:
        # Kernel workspaces may live outside the SUT. There is then no
        # candidate subtree to exclude from the project baseline.
        excluded = None
    for relative_root in _BASELINE_SOURCE_ROOTS:
        _add_baseline_tree(workspace, relative_root, excluded=excluded, manifest=manifest)
    try:
        support_files = collect_test_support_files(workspace, preserve_paths=True)
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
    excluded: Path | None,
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
    model: type[Any] | None = None,
) -> RunTestsInputV1:
    model_dump = getattr(data, "model_dump", None)
    if callable(model_dump):
        data = model_dump(mode="json")
    root = validate_model(ExecutionPrepareInputV1, data)
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
    _authenticate_generation_sources(root, workspace)
    selected = SelectedTargets(
        **{family: family in root.selected_test_families for family in ("api", "e2e", "fuzz", "performance")}
    )
    mappings: list[dict[str, object]] = []
    for family in root.selected_test_families:
        relative = qa_join(f"codegen/{family}-generated-files.json")
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
        capability_leafs=root.capability_leafs,
        reviewed_case=(None if root.generation_result is None else root.generation_result.reviewed_case),
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
        view = lock_durable_execution(
            workspace,
            write_root=write_root,
            change_id=root.change_id,
            batch_id=batch_id,
            merged=merged,
            selected=closed.selected,
        )
        execution_view_root = view.root
        if view.executed_at is None:
            raise ValueError("durable execution lock time is missing")
    except ValueError as error:
        raise InputError(str(error)) from error
    del execution_view_root, model
    return RunTestsInputV1(
        change_id=root.change_id,
        plan_digest=root.plan_digest,
        plan_ref=root.plan_ref,
        batch_id=batch_id,
        executed_at=view.executed_at,
        capability_leafs=root.capability_leafs,
        case_ids=case_ids,
        mapping=closed,
        selected_targets=selected,
        baseline_tree_id=locked_baseline,
        runner_profile_digest=runner_profile_digest,
        coverage_epoch=root.coverage_epoch,
        execution_kind=root.execution_kind,
        timeout_seconds=root.timeout_seconds,
        method_plan_refs=(
            () if root.generation_result is None else (root.generation_result.method_plan_ref,)
        ),
        allowed_origins=root.allowed_origins,
    )


def _finalize_payload(
    data: object,
) -> AgentFinalizeInputV1:
    return validate_model(AgentFinalizeInputV1, data)


def _commit_execution_evidence(
    payload: AgentFinalizeInputV1,
    evidence: ExecutionEvidenceV1,
    *,
    project_root: Path,
    write_root: Path,
    filename: str,
) -> None:
    if payload.execution_view_root != "qa":
        raise OutputError("execution view root is not the durable qa tree")
    try:
        write_root.resolve().relative_to(project_root.resolve())
    except ValueError as error:
        raise OutputError("execution evidence escapes the authenticated attempt workspace") from error
    try:
        families = tuple(dict.fromkeys(item.layer for item in payload.mapping.mappings))
        authenticate_durable_execution(
            project_root,
            write_root=write_root,
            change_id=payload.change_id,
            view=ExecutionView(
                batch_id=payload.batch_id,
                root=payload.execution_view_root,
                selected_targets=payload.mapping.selected,
                digest=payload.execution_view_digest,
                executed_at=payload.executed_at,
            ),
            merged=merge_generated(project_root, payload.change_id, families),
        )
    except (OSError, ValueError) as error:
        raise OutputError(str(error)) from error
    output = resolve_canonical_evidence(write_root, evidence.change_id, filename)
    expected = json.dumps(evidence.model_dump(mode="json"), indent=2).encode("utf-8") + b"\n"
    if output.exists():
        if (
            output.is_symlink()
            or not output.is_file()
            or output.stat().st_nlink != 1
            or output.read_bytes() != expected
        ):
            raise OutputError("prepared execution evidence drifted before finalize recovery")
        return
    write_canonical_evidence(write_root, evidence, filename=filename)


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
