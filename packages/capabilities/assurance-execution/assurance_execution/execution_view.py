from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from pydantic import AwareDatetime

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.qa_paths import qa_join

from assurance_execution.contracts.selection import selected_test_file
from assurance_execution.generated_merge import MergedGeneratedSet

_TESTS_PREFIX = "tests/"
_QA_TESTS_PREFIX = "qa/tests/"
_DURABLE_ROOT = "qa"
_PREPARATION_FILE = ".assurance/execution-prepared-v1.json"
_DURABLE_LOCK_FILE = "durable-execution-v1.json"
_IGNORED_SUPPORT_DIRECTORIES = frozenset(
    {
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_MAX_SUPPORT_FILES = 1_024
_MAX_SUPPORT_FILE_BYTES = 2 * 1024 * 1024
_MAX_SUPPORT_BYTES = 16 * 1024 * 1024


class ExecutionViewPreparation(FrozenModel):
    schema_version: Literal["1"] = "1"
    change_id: str
    batch_id: str
    executed_at: AwareDatetime


class DurableExecutionLock(FrozenModel):
    schema_version: Literal["1"] = "1"
    change_id: str
    batch_id: str
    executed_at: AwareDatetime
    content_digest: str


class ExecutionView(FrozenModel):
    batch_id: str
    root: str
    selected_targets: tuple[str, ...]
    digest: str
    executed_at: AwareDatetime | None = None


def execution_view_relative(batch_id: str) -> str:
    return f"{qa_join('.staging/execution')}/{_safe_component(batch_id, label='batch_id')}"


def build_execution_view(
    project_root: Path,
    *,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
    write_root: Path | None = None,
) -> ExecutionView:
    project = Path(project_root)
    destination_root = project if write_root is None else Path(write_root)
    view, planned = _planned_view(project, change_id, batch_id, merged, selected)
    view_root = _join(destination_root, view.root)
    if view_root.exists():
        raise ValueError(f"conflict: execution view already exists: {view.root}")
    _materialize(view_root, planned)
    return view


def lock_durable_execution(
    project_root: Path,
    *,
    write_root: Path,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> ExecutionView:
    project = Path(project_root)
    destination = Path(write_root)
    closed_change = _safe_component(change_id, label="change_id")
    closed_batch = _safe_component(batch_id, label="batch_id")
    selected_targets = tuple(selected)
    planned = _plan_durable_files(project, merged, selected_targets)
    digest = _planned_digest(planned)
    lock_dir = _join(destination, qa_join(".staging/execution"))
    lock_path = lock_dir / _DURABLE_LOCK_FILE
    if lock_path.exists():
        lock = _read_durable_lock(lock_path, change_id=closed_change)
        if lock.batch_id == closed_batch and lock.content_digest != digest:
            raise ValueError("durable execution digest drifted")
        if lock.content_digest == digest and lock.batch_id == closed_batch:
            executed_at = lock.executed_at
        else:
            executed_at = datetime.now(UTC)
            _write_durable_lock(
                lock_path,
                DurableExecutionLock(
                    change_id=closed_change,
                    batch_id=closed_batch,
                    executed_at=executed_at,
                    content_digest=digest,
                ),
            )
    else:
        executed_at = datetime.now(UTC)
        _write_durable_lock(
            lock_path,
            DurableExecutionLock(
                change_id=closed_change,
                batch_id=closed_batch,
                executed_at=executed_at,
                content_digest=digest,
            ),
        )
    return ExecutionView(
        batch_id=closed_batch,
        root=_DURABLE_ROOT,
        selected_targets=selected_targets,
        digest=digest,
        executed_at=executed_at,
    )


def build_or_authenticate_execution_view(
    project_root: Path,
    *,
    write_root: Path,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> ExecutionView:
    project = Path(project_root)
    destination_root = Path(write_root)
    relative_root = execution_view_relative(batch_id)
    view_root = _join(destination_root, relative_root)
    preparation = (
        _read_preparation(view_root, change_id=change_id, batch_id=batch_id)
        if view_root.exists()
        else ExecutionViewPreparation(
            change_id=change_id,
            batch_id=batch_id,
            executed_at=datetime.now(UTC),
        )
    )
    view, planned = _planned_view(
        project,
        change_id,
        batch_id,
        merged,
        selected,
        preparation=preparation,
    )
    if view_root.exists():
        _authenticate_planned(view_root, planned, view.digest)
        return view
    _materialize(view_root, planned)
    return view


def authenticate_execution_view(write_root: Path, view: ExecutionView) -> None:
    root = _validated_view_root(Path(write_root), view)
    actual = _scan_view(root)
    if _planned_digest(actual) != view.digest:
        raise ValueError(f"execution view digest drifted: {view.root}")


def discard_authenticated_execution_view(write_root: Path, view: ExecutionView) -> None:
    authenticate_execution_view(write_root, view)
    discard_execution_view(write_root, view)


def discard_execution_view(write_root: Path, view: ExecutionView) -> None:
    root = _validated_view_root(Path(write_root), view)
    if root.exists():
        shutil.rmtree(root)


def _planned_view(
    project: Path,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
    preparation: ExecutionViewPreparation | None = None,
) -> tuple[ExecutionView, dict[str, tuple[bytes, int]]]:
    closed_change = _safe_component(change_id, label="change_id")
    closed_batch = _safe_component(batch_id, label="batch_id")
    selected_targets = tuple(selected)
    relative_root = execution_view_relative(closed_batch)
    planned = _plan_view_files(project, merged, selected_targets)
    if preparation is not None:
        if preparation.change_id != closed_change or preparation.batch_id != closed_batch:
            raise ValueError("execution view preparation identity drifted")
        planned[_PREPARATION_FILE] = (
            canonical_json_bytes(cast(JSONValue, preparation.model_dump(mode="json"))),
            0o444,
        )
        planned = dict(sorted(planned.items()))
    return (
        ExecutionView(
            batch_id=closed_batch,
            root=relative_root,
            selected_targets=selected_targets,
            digest=_planned_digest(planned),
            executed_at=preparation.executed_at if preparation is not None else None,
        ),
        planned,
    )


def _read_preparation(
    view_root: Path,
    *,
    change_id: str,
    batch_id: str,
) -> ExecutionViewPreparation:
    path = _join(view_root, _PREPARATION_FILE)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("execution view preparation is missing")
    payload = path.read_bytes()
    try:
        preparation = ExecutionViewPreparation.model_validate_json(payload)
    except ValueError as error:
        raise ValueError("execution view preparation is invalid") from error
    canonical = canonical_json_bytes(cast(JSONValue, preparation.model_dump(mode="json")))
    if payload != canonical:
        raise ValueError("execution view preparation is not canonical")
    if preparation.change_id != change_id or preparation.batch_id != batch_id:
        raise ValueError("execution view preparation identity drifted")
    return preparation


def _materialize(view_root: Path, planned: dict[str, tuple[bytes, int]]) -> None:
    created = False
    try:
        view_root.parent.mkdir(parents=True, exist_ok=True)
        view_root.mkdir()
        created = True
        for relative, (payload, mode) in planned.items():
            destination = _join(view_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            destination.chmod(mode)
    except OSError as error:
        if created and view_root.exists():
            shutil.rmtree(view_root, ignore_errors=True)
        raise ValueError("could not materialize execution view") from error


def _authenticate_planned(
    view_root: Path,
    planned: dict[str, tuple[bytes, int]],
    expected_digest: str,
) -> None:
    actual = _scan_view(view_root)
    if actual != planned or _planned_digest(actual) != expected_digest:
        raise ValueError(f"execution view digest drifted: {view_root.name}")


def _scan_view(root: Path) -> dict[str, tuple[bytes, int]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("execution view is not a real directory")
    scanned: dict[str, tuple[bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("execution view contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("execution view contains a non-regular file")
        scanned[path.relative_to(root).as_posix()] = (
            path.read_bytes(),
            path.stat().st_mode & 0o777,
        )
    return scanned


def _validated_view_root(write_root: Path, view: ExecutionView) -> Path:
    batch_id = _batch_id_from_root(view.root)
    expected = execution_view_relative(batch_id)
    if view.root != expected or batch_id != view.batch_id:
        raise ValueError(f"execution view root is not disposable staging: {view.root}")
    root = _join(write_root, view.root)
    try:
        root.resolve().relative_to(write_root.resolve())
    except ValueError as error:
        raise ValueError(f"execution view root escapes the write root: {view.root}") from error
    return root


def _safe_component(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path component")
    if "\x00" in value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a safe path component")
    return value


def _batch_id_from_root(root: str) -> str:
    parts = PurePosixPath(root).parts
    if len(parts) != 4 or parts[0] != "qa" or parts[1] != ".staging" or parts[2] != "execution":
        raise ValueError(f"execution view root is not disposable staging: {root}")
    return _safe_component(parts[3], label="batch_id")


def _plan_durable_files(
    project: Path,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> dict[str, tuple[bytes, int]]:
    planned: dict[str, tuple[bytes, int]] = {}
    selected_files = tuple(dict.fromkeys(selected_test_file(item) for item in selected))
    for item in merged.files:
        planned[item.target_path] = _read_regular(project, item.staged_path)
    for relative in selected_files:
        if relative not in planned:
            planned[relative] = _read_regular(project, relative)
    for support, source in collect_test_support_files(project).items():
        planned.setdefault(support, source)
    return dict(sorted(planned.items()))


def _write_durable_lock(path: Path, lock: DurableExecutionLock) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o644)
            path.unlink()
        path.write_bytes(canonical_json_bytes(cast(JSONValue, lock.model_dump(mode="json"))))
        path.chmod(0o444)
    except OSError as error:
        raise ValueError("could not persist durable execution lock") from error


def _read_durable_lock(
    path: Path,
    *,
    change_id: str,
) -> DurableExecutionLock:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("durable execution lock is missing")
    payload = path.read_bytes()
    try:
        lock = DurableExecutionLock.model_validate_json(payload)
    except ValueError as error:
        raise ValueError("durable execution lock is invalid") from error
    canonical = canonical_json_bytes(cast(JSONValue, lock.model_dump(mode="json")))
    if payload != canonical:
        raise ValueError("durable execution lock is not canonical")
    if lock.change_id != change_id:
        raise ValueError("durable execution lock identity drifted")
    return lock


def _plan_view_files(
    project: Path,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> dict[str, tuple[bytes, int]]:
    planned: dict[str, tuple[bytes, int]] = {}
    selected_files = tuple(dict.fromkeys(selected_test_file(item) for item in selected))
    for item in merged.files:
        target = _view_target_from_durable(item.target_path)
        planned[target] = _read_regular(project, item.staged_path)
    for relative in selected_files:
        target = _view_target_from_durable(relative)
        if target in planned:
            continue
        planned[target] = _read_regular(project, _durable_from_view_target(target))
    for support, source in collect_test_support_files(project).items():
        target = _view_target_from_durable(support)
        if target not in planned:
            planned[target] = source
    return dict(sorted(planned.items()))


def _view_target_from_durable(relative: str) -> str:
    if relative.startswith(_QA_TESTS_PREFIX):
        relative = _TESTS_PREFIX + relative[len(_QA_TESTS_PREFIX) :]
    return _require_test_support(relative)


def _durable_from_view_target(relative: str) -> str:
    target = _require_test_support(relative)
    return _QA_TESTS_PREFIX + target[len(_TESTS_PREFIX) :]


def _require_test_support(relative: str) -> str:
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if (
        not relative
        or relative.startswith(("/", "~"))
        or "\\" in relative
        or "\x00" in relative
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or posix.as_posix() != relative
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError(f"conflict: path must be canonical and relative: {relative}")
    if not relative.startswith(_TESTS_PREFIX):
        raise ValueError(f"conflict: path is not a test or support file: {relative}")
    return relative


def collect_test_support_files(project: Path) -> dict[str, tuple[bytes, int]]:
    support: dict[str, tuple[bytes, int]] = {}
    for root in (project / "qa" / "tests", project / "qa" / "fixtures"):
        for relative, source in _collect_support_tree(project, root).items():
            support.setdefault(relative, source)
    return support


def _collect_support_tree(project: Path, root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("conflict: test support root is not a real directory")
    support: dict[str, tuple[bytes, int]] = {}
    total_bytes = 0
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            if name in _IGNORED_SUPPORT_DIRECTORIES:
                continue
            path = current_path / name
            if path.is_symlink():
                raise ValueError("conflict: test support namespace contains a symbolic link")
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames):
            if not _is_python_support(name):
                continue
            source_relative = (current_path / name).relative_to(project).as_posix()
            relative = _QA_TESTS_PREFIX + (current_path / name).relative_to(root).as_posix()
            source = _read_regular(project, source_relative)
            payload, _ = source
            if len(payload) > _MAX_SUPPORT_FILE_BYTES:
                raise ValueError(f"conflict: test support file exceeds size limit: {relative}")
            support[relative] = source
            total_bytes += len(payload)
            if len(support) > _MAX_SUPPORT_FILES or total_bytes > _MAX_SUPPORT_BYTES:
                raise ValueError("conflict: test support files exceed execution view limits")
    return support


def _is_python_support(filename: str) -> bool:
    return (
        filename.endswith(".py")
        and not filename.startswith("test_")
        and not filename.endswith("_test.py")
        and not filename.startswith("locustfile_")
    )


def _read_regular(project: Path, relative: str) -> tuple[bytes, int]:
    path = _join(project, relative)
    try:
        path.resolve().relative_to(project.resolve())
    except ValueError as error:
        raise ValueError(f"conflict: path escapes the project: {relative}") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"conflict: source is not a regular single-link file: {relative}")
    return path.read_bytes(), path.stat().st_mode & 0o777


def _join(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _planned_digest(planned: dict[str, tuple[bytes, int]]) -> str:
    return canonical_digest(
        [[relative, _digest_bytes(payload), mode] for relative, (payload, mode) in planned.items()]
    )


__all__ = [
    "DurableExecutionLock",
    "ExecutionView",
    "ExecutionViewPreparation",
    "authenticate_execution_view",
    "build_execution_view",
    "build_or_authenticate_execution_view",
    "collect_test_support_files",
    "discard_authenticated_execution_view",
    "discard_execution_view",
    "execution_view_relative",
    "lock_durable_execution",
]
