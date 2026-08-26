from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.selection import selected_test_file
from assurance_product.change_workspace import safe_change_id, safe_relative_path
from assurance_product.generated_merge import MergedGeneratedSet

_TESTS_PREFIX = "tests/"


class ExecutionView(FrozenModel):
    batch_id: str
    root: str
    selected_targets: tuple[str, ...]
    digest: str


def execution_view_relative(change_id: str, batch_id: str) -> str:
    return f"qa/changes/{safe_change_id(change_id)}/.staging/execution/{_safe_batch_id(batch_id)}"


def build_execution_view(
    project_root: Path,
    *,
    change_id: str,
    batch_id: str,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> ExecutionView:
    project = Path(project_root)
    closed_change = safe_change_id(change_id)
    closed_batch = _safe_batch_id(batch_id)
    selected_targets = tuple(selected)
    relative_root = execution_view_relative(closed_change, closed_batch)
    view_root = _join(project, relative_root)
    if view_root.exists():
        raise ValueError(f"conflict: execution view already exists: {relative_root}")
    planned = _plan_view_files(project, merged, selected_targets)
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
    digest = canonical_digest(
        [[relative, _digest_bytes(payload), mode] for relative, (payload, mode) in planned.items()]
    )
    return ExecutionView(
        batch_id=closed_batch,
        root=relative_root,
        selected_targets=selected_targets,
        digest=digest,
    )


def discard_execution_view(project_root: Path, view: ExecutionView) -> None:
    project = Path(project_root)
    root = _join(project, view.root)
    expected = execution_view_relative(safe_change_id(_change_id_from_root(view.root)), view.batch_id)
    if view.root != expected:
        raise ValueError(f"execution view root is not disposable staging: {view.root}")
    if root.exists():
        shutil.rmtree(root)


def _safe_batch_id(batch_id: str) -> str:
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("batch_id must be a non-empty path component")
    if "\x00" in batch_id or batch_id in {".", ".."}:
        raise ValueError("batch_id must be a safe path component")
    if "/" in batch_id or "\\" in batch_id:
        raise ValueError("batch_id must not contain path separators")
    return batch_id


def _change_id_from_root(root: str) -> str:
    parts = PurePosixPath(root).parts
    if len(parts) < 5 or parts[0] != "qa" or parts[1] != "changes" or parts[3] != ".staging":
        raise ValueError(f"execution view root is not disposable staging: {root}")
    return parts[2]


def _plan_view_files(
    project: Path,
    merged: MergedGeneratedSet,
    selected: tuple[str, ...],
) -> dict[str, tuple[bytes, int]]:
    planned: dict[str, tuple[bytes, int]] = {}
    selected_files = tuple(dict.fromkeys(selected_test_file(item) for item in selected))
    for item in merged.files:
        target = _require_test_support(item.target_path)
        planned[target] = _read_regular(project, item.staged_path)
    for relative in selected_files:
        target = _require_test_support(relative)
        if target in planned:
            continue
        planned[target] = _read_regular(project, target)
    for relative in tuple(planned):
        for support in _support_files(relative):
            source = _join(project, support)
            if support in planned or not source.is_file():
                continue
            planned[support] = _read_regular(project, support)
    return dict(sorted(planned.items()))


def _require_test_support(relative: str) -> str:
    target = safe_relative_path(relative).as_posix()
    if target != relative or "\\" in relative:
        raise ValueError(f"conflict: path must be canonical and relative: {relative}")
    if not target.startswith(_TESTS_PREFIX):
        raise ValueError(f"conflict: path is not a test or support file: {relative}")
    return target


def _support_files(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != "tests":
        return ()
    supports: list[str] = []
    for index in range(1, len(parts)):
        supports.append(str(PurePosixPath(*parts[:index], "conftest.py")))
    return tuple(supports)


def _read_regular(project: Path, relative: str) -> tuple[bytes, int]:
    path = _join(project, relative)
    try:
        resolved = path.resolve()
        resolved.relative_to(project.resolve())
    except ValueError as error:
        raise ValueError(f"conflict: path escapes the project: {relative}") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"conflict: source is not a regular single-link file: {relative}")
    return path.read_bytes(), path.stat().st_mode & 0o777


def _join(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ExecutionView",
    "build_execution_view",
    "discard_execution_view",
    "execution_view_relative",
]
