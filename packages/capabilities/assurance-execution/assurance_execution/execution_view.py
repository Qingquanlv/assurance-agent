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

from assurance_execution.contracts.selection import selected_test_file
from assurance_execution.generated_merge import ExecutionViewInputV1, MergedGeneratedSet

_TESTS_PREFIX = "tests/"
_PREPARATION_FILE = ".assurance/execution-prepared-v1.json"
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
    mode: Literal["legacy", "verified"] = "legacy"
    execution_id: str | None = None


class ExecutionView(FrozenModel):
    batch_id: str
    root: str
    selected_targets: tuple[str, ...]
    digest: str
    executed_at: AwareDatetime | None = None
    mode: Literal["legacy", "verified"] = "legacy"
    execution_id: str | None = None


def execution_view_relative(change_id: str, batch_id: str, execution_id: str | None = None) -> str:
    base = (
        f"qa/changes/{_safe_component(change_id, label='change_id')}"
        f"/.staging/execution/{_safe_component(batch_id, label='batch_id')}"
    )
    if execution_id is None:
        return base
    return f"{base}/{_safe_component(execution_id, label='execution_id')}"


def build_execution_view(
    project_root: Path,
    *,
    change_id: str | None = None,
    batch_id: str | None = None,
    merged: MergedGeneratedSet | None = None,
    selected: tuple[str, ...] | None = None,
    request: ExecutionViewInputV1 | None = None,
    write_root: Path | None = None,
) -> ExecutionView:
    project = Path(project_root)
    destination_root = project if write_root is None else Path(write_root)
    identity = _resolve_input(change_id, batch_id, merged, selected, request)
    view, planned = _planned_view(project, *identity)
    view_root = _join(destination_root, view.root)
    if view_root.exists():
        raise ValueError(f"conflict: execution view already exists: {view.root}")
    _materialize(view_root, planned, readonly=view.mode == "verified")
    return view


def build_or_authenticate_execution_view(
    project_root: Path,
    *,
    write_root: Path,
    change_id: str | None = None,
    batch_id: str | None = None,
    merged: MergedGeneratedSet | None = None,
    selected: tuple[str, ...] | None = None,
    request: ExecutionViewInputV1 | None = None,
) -> ExecutionView:
    project = Path(project_root)
    destination_root = Path(write_root)
    closed_change, closed_batch, closed_merged, closed_selected, mode, execution_id = _resolve_input(
        change_id, batch_id, merged, selected, request
    )
    relative_root = execution_view_relative(closed_change, closed_batch, execution_id)
    view_root = _join(destination_root, relative_root)
    preparation = (
        _read_preparation(
            view_root,
            change_id=closed_change,
            batch_id=closed_batch,
            mode=mode,
            execution_id=execution_id,
        )
        if view_root.exists()
        else ExecutionViewPreparation(
            change_id=closed_change,
            batch_id=closed_batch,
            executed_at=datetime.now(UTC),
            mode=mode,
            execution_id=execution_id,
        )
    )
    view, planned = _planned_view(
        project,
        closed_change,
        closed_batch,
        closed_merged,
        closed_selected,
        mode,
        execution_id,
        preparation=preparation,
    )
    if view_root.exists():
        _authenticate_planned(
            view_root,
            planned,
            view.digest,
            mode=view.mode,
            execution_id=view.execution_id,
        )
        return view
    _materialize(view_root, planned, readonly=view.mode == "verified")
    return view


def authenticate_execution_view(write_root: Path, view: ExecutionView) -> None:
    root = _validated_view_root(Path(write_root), view)
    actual = _scan_view(root)
    if view.mode == "verified":
        _authenticate_readonly_tree(root)
    if _planned_digest(actual, mode=view.mode, execution_id=view.execution_id) != view.digest:
        raise ValueError(f"execution view digest drifted: {view.root}")


def discard_authenticated_execution_view(write_root: Path, view: ExecutionView) -> None:
    authenticate_execution_view(write_root, view)
    discard_execution_view(write_root, view)


def discard_execution_view(write_root: Path, view: ExecutionView) -> None:
    root = _validated_view_root(Path(write_root), view)
    if root.exists():
        if view.mode == "verified":
            for directory in (root, *tuple(path for path in root.rglob("*") if path.is_dir())):
                directory.chmod(0o755)
        shutil.rmtree(root)


def _planned_view(
    project: Path,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
    mode: Literal["legacy", "verified"] = "legacy",
    execution_id: str | None = None,
    preparation: ExecutionViewPreparation | None = None,
) -> tuple[ExecutionView, dict[str, tuple[bytes, int]]]:
    closed_change = _safe_component(change_id, label="change_id")
    closed_batch = _safe_component(batch_id, label="batch_id")
    selected_targets = tuple(selected)
    relative_root = execution_view_relative(closed_change, closed_batch, execution_id)
    planned = _plan_view_files(project, merged, selected_targets, readonly=mode == "verified")
    if preparation is not None:
        if (
            preparation.change_id != closed_change
            or preparation.batch_id != closed_batch
            or preparation.mode != mode
            or preparation.execution_id != execution_id
        ):
            raise ValueError("execution view preparation identity drifted")
        planned[_PREPARATION_FILE] = (
            _preparation_bytes(preparation),
            0o444,
        )
        planned = dict(sorted(planned.items()))
    return (
        ExecutionView(
            batch_id=closed_batch,
            root=relative_root,
            selected_targets=selected_targets,
            digest=_planned_digest(planned, mode=mode, execution_id=execution_id),
            executed_at=preparation.executed_at if preparation is not None else None,
            mode=mode,
            execution_id=execution_id,
        ),
        planned,
    )


def _read_preparation(
    view_root: Path,
    *,
    change_id: str,
    batch_id: str,
    mode: Literal["legacy", "verified"],
    execution_id: str | None,
) -> ExecutionViewPreparation:
    path = _join(view_root, _PREPARATION_FILE)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("execution view preparation is missing")
    payload = path.read_bytes()
    try:
        preparation = ExecutionViewPreparation.model_validate_json(payload)
    except ValueError as error:
        raise ValueError("execution view preparation is invalid") from error
    canonical = _preparation_bytes(preparation)
    if payload != canonical:
        raise ValueError("execution view preparation is not canonical")
    if (
        preparation.change_id != change_id
        or preparation.batch_id != batch_id
        or preparation.mode != mode
        or preparation.execution_id != execution_id
    ):
        raise ValueError("execution view preparation identity drifted")
    return preparation


def _materialize(view_root: Path, planned: dict[str, tuple[bytes, int]], *, readonly: bool = False) -> None:
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
        if readonly:
            for directory in sorted(
                (path for path in view_root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.chmod(0o555)
            view_root.chmod(0o555)
    except OSError as error:
        if created and view_root.exists():
            shutil.rmtree(view_root, ignore_errors=True)
        raise ValueError("could not materialize execution view") from error


def _authenticate_planned(
    view_root: Path,
    planned: dict[str, tuple[bytes, int]],
    expected_digest: str,
    *,
    mode: Literal["legacy", "verified"],
    execution_id: str | None,
) -> None:
    actual = _scan_view(view_root)
    if mode == "verified":
        _authenticate_readonly_tree(view_root)
    if actual != planned or _planned_digest(actual, mode=mode, execution_id=execution_id) != expected_digest:
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
    change_id = _change_id_from_root(view.root)
    expected = execution_view_relative(change_id, view.batch_id, view.execution_id)
    if view.root != expected:
        raise ValueError(f"execution view root is not disposable staging: {view.root}")
    if (view.mode == "verified") != (view.execution_id is not None):
        raise ValueError("execution view mode and identity conflict")
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


def _change_id_from_root(root: str) -> str:
    parts = PurePosixPath(root).parts
    if (
        len(parts) not in {6, 7}
        or parts[0] != "qa"
        or parts[1] != "changes"
        or parts[3] != ".staging"
        or parts[4] != "execution"
    ):
        raise ValueError(f"execution view root is not disposable staging: {root}")
    return _safe_component(parts[2], label="change_id")


def _plan_view_files(
    project: Path,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
    *,
    readonly: bool,
) -> dict[str, tuple[bytes, int]]:
    planned: dict[str, tuple[bytes, int]] = {}
    selected_files = tuple(dict.fromkeys(selected_test_file(item) for item in selected))
    for item in merged.files:
        target = _require_test_support(item.target_path)
        payload, mode = _read_regular(project, item.staged_path)
        if _digest_bytes(payload) != item.sha256 or mode != item.mode:
            raise ValueError(f"generated descriptor does not match workspace bytes: {target}")
        planned[target] = (payload, 0o444 if readonly else mode)
    for relative in selected_files:
        target = _require_test_support(relative)
        if target in planned:
            continue
        payload, mode = _read_regular(project, target)
        planned[target] = (payload, 0o444 if readonly else mode)
    for support, source in collect_test_support_files(project).items():
        if support not in planned:
            payload, mode = source
            planned[support] = (payload, 0o444 if readonly else mode)
    return dict(sorted(planned.items()))


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
    root = project / "tests"
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
            relative = (current_path / name).relative_to(project).as_posix()
            source = _read_regular(project, relative)
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


def _planned_digest(
    planned: dict[str, tuple[bytes, int]],
    *,
    mode: Literal["legacy", "verified"] = "legacy",
    execution_id: str | None = None,
) -> str:
    files = [
        [relative, _digest_bytes(payload), file_mode] for relative, (payload, file_mode) in planned.items()
    ]
    if mode == "legacy":
        if execution_id is not None:
            raise ValueError("legacy execution view cannot have an execution identity")
        return canonical_digest(cast(JSONValue, files))
    if execution_id is None:
        raise ValueError("verified execution view requires an execution identity")
    return canonical_digest(
        cast(
            JSONValue,
            {
                "mode": mode,
                "execution_id": execution_id,
                "files": files,
            },
        )
    )


def _preparation_bytes(preparation: ExecutionViewPreparation) -> bytes:
    excluded = {"mode", "execution_id"} if preparation.mode == "legacy" else set()
    payload = preparation.model_dump(mode="json", exclude=excluded)
    return canonical_json_bytes(cast(JSONValue, payload))


def _resolve_input(
    change_id: str | None,
    batch_id: str | None,
    merged: MergedGeneratedSet | None,
    selected: tuple[str, ...] | None,
    request: ExecutionViewInputV1 | None,
) -> tuple[
    str,
    str,
    MergedGeneratedSet,
    tuple[str, ...],
    Literal["legacy", "verified"],
    str | None,
]:
    if request is not None:
        if any(value is not None for value in (change_id, batch_id, merged, selected)):
            raise ValueError("verified execution view input cannot be mixed with legacy arguments")
        return (
            request.change_id,
            request.batch_id,
            MergedGeneratedSet(files=request.generated_files, digest=request.generated_digest),
            request.selected,
            "verified",
            request.execution_id,
        )
    if change_id is None or batch_id is None or merged is None or selected is None:
        raise ValueError("legacy execution view arguments are incomplete")
    return change_id, batch_id, merged, tuple(selected), "legacy", None


def _authenticate_readonly_tree(root: Path) -> None:
    directories = (root, *tuple(path for path in root.rglob("*") if path.is_dir()))
    if any(path.stat().st_mode & 0o777 != 0o555 for path in directories):
        raise ValueError("verified execution view directory permissions drifted")


__all__ = [
    "ExecutionView",
    "ExecutionViewInputV1",
    "ExecutionViewPreparation",
    "authenticate_execution_view",
    "build_execution_view",
    "build_or_authenticate_execution_view",
    "collect_test_support_files",
    "discard_authenticated_execution_view",
    "discard_execution_view",
    "execution_view_relative",
]
