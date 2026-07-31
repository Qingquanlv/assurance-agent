"""Bounded durable revision views and exact manual plan-revision capture.

Transport lives under ``.graph-runtime/revision-views/<interrupt-id>/`` and is
never evidence. Accepted revisions are published only as immutable TreeStore
objects derived from exact allowlisted plan bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.workflow.graph.contracts import ResourcePath
from assurance_agent.workflow.graph.workspace import TreeFileRevision, TreeStore

_REVISION_VIEWS_RELPATH = PurePosixPath(".graph-runtime") / "revision-views"
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class ManualRevisionError(AaError):
    """Bounded revision-view inventory or candidate validation failure."""


@dataclass(frozen=True, slots=True)
class RevisionPathBaseline:
    logical_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RevisionViewBinding:
    interrupt_id: str
    owner_invocation_id: str
    base_tree_id: str
    view_relpath: str
    logical_paths: tuple[str, ...]
    baseline: tuple[RevisionPathBaseline, ...]


def _after_revision_inventory_hook(view_root: Path) -> None:
    """Test seam between inventory validation and descriptor-bound reads."""
    del view_root


def _view_relpath_for(interrupt_id: str) -> str:
    assert_path_segment_safe(interrupt_id, label="interrupt id")
    return (_REVISION_VIEWS_RELPATH / interrupt_id).as_posix()


def _canonical_logical_paths(logical_paths: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    canonical: list[str] = []
    for raw in logical_paths:
        path = ResourcePath.parse(raw)
        if path.root != "change" or not path.pattern.startswith("plans/"):
            raise ManualRevisionError(f"revision view path must stay under change:plans/: {raw}")
        if path.pattern.endswith("/") or path.pattern == "plans":
            raise ManualRevisionError(f"revision view path must be a file: {raw}")
        key = f"{path.root}:{path.pattern}"
        if key in seen:
            raise ManualRevisionError(f"duplicate revision view path: {key}")
        seen.add(key)
        canonical.append(key)
    return tuple(sorted(canonical))


def _relative_for(logical_path: str) -> str:
    return ResourcePath.parse(logical_path).pattern


def _expected_inventory(logical_paths: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    files = frozenset(_relative_for(path) for path in logical_paths)
    dirs: set[str] = set()
    for rel in files:
        parts = PurePosixPath(rel).parts
        for index in range(1, len(parts)):
            dirs.add(PurePosixPath(*parts[:index]).as_posix())
    return files, frozenset(dirs)


def _open_dir_nofollow(parent_fd: int, name: str) -> int:
    return os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)


def _open_view_root(change_dir: Path, view_relpath: str) -> int:
    fd = os.open(change_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in PurePosixPath(view_relpath).parts:
            next_fd = _open_dir_nofollow(fd, component)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise ManualRevisionError(f"revision view root unavailable: {view_relpath}") from exc


def _inventory_revision_view(view_fd: int) -> tuple[frozenset[str], frozenset[str]]:
    files: set[str] = set()
    dirs: set[str] = set()

    def visit(dir_fd: int, prefix: str) -> None:
        try:
            names = os.listdir(dir_fd)
        except OSError as exc:
            raise ManualRevisionError("revision view inventory failed") from exc
        for name in names:
            rel = name if not prefix else f"{prefix}/{name}"
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise ManualRevisionError(f"revision view inventory failed: {rel}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise ManualRevisionError(f"symlink in revision view: {rel}")
            if stat.S_ISDIR(st.st_mode):
                dirs.add(rel)
                child_fd = _open_dir_nofollow(dir_fd, name)
                try:
                    visit(child_fd, rel)
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(st.st_mode):
                files.add(rel)
                continue
            raise ManualRevisionError(f"non-file in revision view: {rel}")

    visit(view_fd, "")
    return frozenset(files), frozenset(dirs)


def _assert_inventory_matches(
    files: frozenset[str],
    dirs: frozenset[str],
    logical_paths: tuple[str, ...],
) -> None:
    expected_files, expected_dirs = _expected_inventory(logical_paths)
    missing = expected_files - files
    extra = files - expected_files
    extra_dirs = dirs - expected_dirs
    if missing:
        raise ManualRevisionError(f"revision view inventory missing allowlisted path: {sorted(missing)[0]}")
    if extra:
        raise ManualRevisionError(f"revision view inventory has extra path: {sorted(extra)[0]}")
    if extra_dirs:
        raise ManualRevisionError(f"revision view inventory has extra directory: {sorted(extra_dirs)[0]}")


def _read_file_nofollow(parent_fd: int, name: str, *, rel: str) -> bytes:
    try:
        fd = os.open(name, _OPEN_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ManualRevisionError(f"symlink or unreadable path in revision view: {rel}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ManualRevisionError(f"non-file in revision view: {rel}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_relative_via_descriptors(view_fd: int, rel: str) -> bytes:
    parts = PurePosixPath(rel).parts
    if not parts:
        raise ManualRevisionError(f"empty revision view path: {rel}")
    fd = view_fd
    opened: list[int] = []
    try:
        for component in parts[:-1]:
            fd = _open_dir_nofollow(fd, component)
            opened.append(fd)
        return _read_file_nofollow(fd, parts[-1], rel=rel)
    except OSError as exc:
        raise ManualRevisionError(f"symlink or unreadable path in revision view: {rel}") from exc
    finally:
        for child_fd in reversed(opened):
            os.close(child_fd)


def _read_validated_revision_files(
    change_dir: Path,
    binding: RevisionViewBinding,
) -> Mapping[str, bytes]:
    logical_paths = _canonical_logical_paths(tuple(binding.logical_paths))
    if tuple(binding.logical_paths) != logical_paths:
        raise ManualRevisionError("revision view binding logical_paths must be canonical")

    view_root = change_dir / binding.view_relpath
    view_fd = _open_view_root(change_dir, binding.view_relpath)
    try:
        files, dirs = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files, dirs, logical_paths)
        _after_revision_inventory_hook(view_root)
        replacements: dict[str, bytes] = {}
        for logical_path in logical_paths:
            rel = _relative_for(logical_path)
            replacements[logical_path] = _read_relative_via_descriptors(view_fd, rel)
        files_after, dirs_after = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files_after, dirs_after, logical_paths)
        return replacements
    finally:
        os.close(view_fd)


def _baseline_for(
    store: TreeStore,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
) -> tuple[RevisionPathBaseline, ...]:
    items: list[RevisionPathBaseline] = []
    for logical_path in logical_paths:
        data = store.read_bytes(base_tree_id, logical_path)
        items.append(
            RevisionPathBaseline(
                logical_path=logical_path,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(items)


def _write_revision_files(
    *,
    change_dir: Path,
    store: TreeStore,
    base_tree_id: str,
    view_relpath: str,
    logical_paths: tuple[str, ...],
) -> None:
    view_root = change_dir / view_relpath
    if view_root.exists() or view_root.is_symlink():
        if view_root.is_symlink() or view_root.is_file():
            view_root.unlink()
        else:
            shutil.rmtree(view_root)
    view_root.mkdir(parents=True, exist_ok=False)
    for logical_path in logical_paths:
        rel = _relative_for(logical_path)
        target = view_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = store.read_bytes(base_tree_id, logical_path)
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)


def _write_or_validate_revision_view(
    *,
    change_dir: Path,
    store: TreeStore,
    interrupt_id: str,
    owner_invocation_id: str,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
    committed_binding: RevisionViewBinding | None,
) -> RevisionViewBinding:
    canonical_paths = _canonical_logical_paths(logical_paths)
    view_relpath = _view_relpath_for(interrupt_id)
    if committed_binding is None:
        _write_revision_files(
            change_dir=change_dir,
            store=store,
            base_tree_id=base_tree_id,
            view_relpath=view_relpath,
            logical_paths=canonical_paths,
        )
        return RevisionViewBinding(
            interrupt_id=interrupt_id,
            owner_invocation_id=owner_invocation_id,
            base_tree_id=base_tree_id,
            view_relpath=view_relpath,
            logical_paths=canonical_paths,
            baseline=_baseline_for(store, base_tree_id, canonical_paths),
        )

    expected = RevisionViewBinding(
        interrupt_id=interrupt_id,
        owner_invocation_id=owner_invocation_id,
        base_tree_id=base_tree_id,
        view_relpath=view_relpath,
        logical_paths=canonical_paths,
        baseline=_baseline_for(store, base_tree_id, canonical_paths),
    )
    if (
        committed_binding.interrupt_id != expected.interrupt_id
        or committed_binding.owner_invocation_id != expected.owner_invocation_id
        or committed_binding.base_tree_id != expected.base_tree_id
        or committed_binding.view_relpath != expected.view_relpath
        or committed_binding.logical_paths != expected.logical_paths
        or committed_binding.baseline != expected.baseline
    ):
        raise ManualRevisionError("committed revision view binding metadata mismatch")
    view_root = change_dir / committed_binding.view_relpath
    if not view_root.is_dir() or view_root.is_symlink():
        raise ManualRevisionError(f"committed revision view missing: {committed_binding.view_relpath}")
    # Preserve user edits; only confirm the path inventory still matches.
    view_fd = _open_view_root(change_dir, committed_binding.view_relpath)
    try:
        files, dirs = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files, dirs, committed_binding.logical_paths)
    finally:
        os.close(view_fd)
    return committed_binding


def materialize_revision_view(
    *,
    change_dir: Path,
    store: TreeStore,
    interrupt_id: str,
    owner_invocation_id: str,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
    committed_binding: RevisionViewBinding | None,
) -> RevisionViewBinding:
    """Materialize or validate the exact bounded transport view."""
    return _write_or_validate_revision_view(
        change_dir=change_dir,
        store=store,
        interrupt_id=interrupt_id,
        owner_invocation_id=owner_invocation_id,
        base_tree_id=base_tree_id,
        logical_paths=logical_paths,
        committed_binding=committed_binding,
    )


def capture_revision_candidate(
    *,
    change_dir: Path,
    store: TreeStore,
    binding: RevisionViewBinding,
) -> TreeFileRevision:
    """Validate the complete view inventory and publish exact replacements."""
    replacements = _read_validated_revision_files(change_dir, binding)
    return store.replace_tree_files(binding.base_tree_id, replacements)


__all__ = [
    "ManualRevisionError",
    "RevisionPathBaseline",
    "RevisionViewBinding",
    "capture_revision_candidate",
    "materialize_revision_view",
]
