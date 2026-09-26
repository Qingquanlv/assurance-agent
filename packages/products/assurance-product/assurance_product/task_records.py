"""Saved task definition and task-scoped run allocation."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.status import read_bootstrap_status, read_run_manifest
from assurance_product.change_workspace import (
    ChangeWorkspace,
    require_descendant,
    require_real_directory,
    safe_change_id,
)
from assurance_product.models import TEST_FAMILY_ORDER


class TaskDefinitionV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    task_id: str = Field(min_length=1)
    project_dir: str = Field(min_length=1)
    task_directory: str = Field(min_length=1)
    name: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    test_families: tuple[str, ...] = Field(min_length=1)
    next_run_number: int = Field(ge=1)


class TaskRecordError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def task_record_path(task_directory: Path) -> Path:
    return task_directory / ".aa" / "task.json"


def without_managed_change_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Drop managed task records from a sealed change-file list."""

    visible: list[str] = []
    for path in paths:
        normal = path.replace("\\", "/")
        if normal.startswith("./"):
            normal = normal[2:]
        if normal == ".aa/task.json" or normal.startswith(".aa/runs/"):
            continue
        visible.append(path)
    return tuple(visible)


def input_digest(
    *,
    task_id: str,
    requirement: str,
    families: tuple[str, ...],
    config_source: str,
    endpoint: str,
) -> str:
    payload: JSONValue = {
        "config_source": config_source,
        "endpoint": endpoint,
        "families": list(families),
        "requirement": requirement,
        "task_id": task_id,
    }
    return canonical_digest(payload)


def define_task(
    *,
    project_dir: Path,
    task_directory: Path,
    name: str,
    base_ref: str,
    requirement: str,
    families: tuple[str, ...],
) -> TaskDefinitionV1:
    project = project_dir.resolve()
    task = task_directory.resolve()
    if not task.is_dir():
        raise TaskRecordError("invalid_input", "task directory does not exist")
    ordered = _ordered_families(families)
    text = requirement.strip()
    if not text or not name.strip() or not base_ref.strip():
        raise TaskRecordError("invalid_input", "task name, base ref, and requirement are required")
    existing = read_task(task)
    if existing is not None:
        if (
            existing.project_dir == str(project)
            and existing.name == name.strip()
            and existing.base_ref == base_ref.strip()
            and existing.requirement == text
            and existing.test_families == ordered
        ):
            _prepare_qa_directory(task)
            return existing
        raise TaskRecordError("invalid_input", "task is already configured")
    _prepare_qa_directory(task)
    definition = TaskDefinitionV1(
        task_id=secrets.token_hex(16),
        project_dir=str(project),
        task_directory=str(task),
        name=name.strip(),
        base_ref=base_ref.strip(),
        requirement=text,
        test_families=ordered,
        next_run_number=1,
    )
    _write_definition(task, definition)
    return definition


def _prepare_qa_directory(task_directory: Path) -> None:
    qa = task_directory / "qa"
    if qa.is_symlink() or (qa.exists() and not qa.is_dir()):
        raise TaskRecordError("invalid_input", "qa directory must be real")
    try:
        qa.mkdir(exist_ok=True)
    except OSError as error:
        raise TaskRecordError("invalid_input", "could not create qa directory") from error


def read_task(task_directory: Path) -> TaskDefinitionV1 | None:
    path = task_record_path(task_directory)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        definition = TaskDefinitionV1.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise TaskRecordError("invalid_input", "invalid task identity") from error
    if Path(definition.task_directory).resolve() != task_directory.resolve():
        raise TaskRecordError("invalid_input", "invalid task identity")
    return definition


def write_next_run_number(
    task_directory: Path, definition: TaskDefinitionV1, number: int
) -> TaskDefinitionV1:
    updated = definition.model_copy(update={"next_run_number": number})
    _write_definition(task_directory, updated)
    return updated


@contextmanager
def allocation_lock(task_directory: Path, task_id: str) -> Iterator[None]:
    """Exclusive lock for one task id. Not a project lock and not an invocation lease."""
    control = task_directory.resolve() / ".aa" / "operator"
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / f"{task_id}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_manifests(task_directory: Path) -> list[dict[str, object]]:
    runs_root = task_directory / ".aa" / "runs"
    if not runs_root.is_dir():
        return []
    found: list[dict[str, object]] = []
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        try:
            manifest = dict(read_run_manifest(run_dir))
        except (OSError, ValueError) as error:
            raise TaskRecordError("recovery", f"run manifest is unreadable: {run_dir.name}") from error
        manifest["run_dir"] = str(run_dir)
        found.append(manifest)
    return found


def active_manifest(manifests: list[dict[str, object]]) -> dict[str, object] | None:
    active: dict[str, object] | None = None
    for manifest in manifests:
        run_dir = manifest.get("run_dir")
        if not isinstance(run_dir, str):
            raise TaskRecordError("recovery", "run ownership is unknown")
        try:
            status = read_bootstrap_status(Path(run_dir))
        except (OSError, ValueError) as error:
            raise TaskRecordError("recovery", "run ownership is unknown") from error
        task_value = manifest.get("task_directory")
        task = Path(task_value) if isinstance(task_value, str) else None
        if _workspace_released(status, task):
            continue
        if active is not None:
            raise TaskRecordError("recovery", "run ownership is unknown")
        active = manifest
    return active


def _workspace_released(status: BootstrapStatusV1, task: Path | None) -> bool:
    if status.phase != "terminal" or status.exit_code is None:
        return False
    graph_status = status.status.get("status")
    if graph_status in {"completed", "succeeded", "failed", "stopped"}:
        return True
    if status.status or task is None:
        return False
    identity = (
        task / "qa" / ".runtime" / "langgraph" / "identities" / f"{safe_change_id(status.change_id)}.json"
    )
    return not identity.exists() and not identity.is_symlink()


def prepare_managed_workspace(task_directory: Path, change_id: str) -> ChangeWorkspace:
    """Retire a known terminal Run's status before reusing its task workspace."""
    task = require_real_directory(task_directory)
    definition = read_task(task)
    if definition is None:
        raise TaskRecordError("not_configured", "task is not configured")
    with allocation_lock(task, definition.task_id):
        _managed_run(definition, change_id)
        _prepare_qa_directory(task)
        status_path = task / "qa" / "status.json"
        if status_path.is_symlink():
            raise TaskRecordError("recovery", "persisted change identity is invalid")
        if not status_path.exists():
            return ChangeWorkspace.prepare(task, change_id)
        try:
            previous_id = json.loads(status_path.read_bytes())["change"]["change_id"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise TaskRecordError("recovery", "persisted change identity is invalid") from error
        workspace = ChangeWorkspace.open(task, previous_id)
        if previous_id == change_id:
            workspace.initialize()
            return workspace
        previous_dir, previous_status = _managed_run(definition, previous_id)
        if not _workspace_released(previous_status, task):
            raise TaskRecordError("recovery", "previous run is not known to be terminal")
        workspace.initialize()
        # link refuses to overwrite history and preserves the exact status bytes.
        archived = previous_dir / "qa-status.json"
        try:
            os.link(status_path, archived)
        except FileExistsError:
            if archived.is_symlink() or not archived.samefile(status_path):
                raise
        status_path.unlink()
        return ChangeWorkspace.prepare(task, change_id)


def _managed_run(definition: TaskDefinitionV1, change_id: str) -> tuple[Path, BootstrapStatusV1]:
    task = Path(definition.task_directory)
    run_dir = require_descendant(task, task / ".aa" / "runs" / safe_change_id(change_id))
    manifest = read_run_manifest(run_dir)
    status = read_bootstrap_status(run_dir)
    if (
        manifest.get("task_id") != definition.task_id
        or manifest.get("task_directory") != str(task)
        or manifest.get("change_id") != change_id
        or status.change_id != change_id
    ):
        raise TaskRecordError("recovery", "run ownership is unknown")
    return run_dir, status


def _ordered_families(families: tuple[str, ...]) -> tuple[str, ...]:
    if not families:
        raise TaskRecordError("invalid_input", "candidate_test_families is required")
    unknown = [name for name in families if name not in TEST_FAMILY_ORDER]
    if unknown:
        raise TaskRecordError("invalid_input", "invalid test family: " + ", ".join(unknown))
    if len(set(families)) != len(families):
        raise TaskRecordError("invalid_input", "candidate_test_families must be unique")
    return tuple(name for name in TEST_FAMILY_ORDER if name in families)


def _write_definition(task_directory: Path, definition: TaskDefinitionV1) -> None:
    path = task_record_path(task_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(definition.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    pending = path.with_name(f".{path.name}.pending")
    pending.write_text(payload, encoding="utf-8")
    os.replace(pending, path)


def managed_baseline(
    changed_files: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
) -> tuple[str, ...]:
    names: list[str] = []
    for item in changed_files:
        path = item.get("path")
        if isinstance(path, str):
            names.append(path)
    return without_managed_change_paths(names)
