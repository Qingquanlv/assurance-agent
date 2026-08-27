"""One-shot descriptor-relative deletion of closed legacy runtime state.

Never imported by ``assurance_product`` and never called by ``aa start``/``run``.
Cleanup requires the exact zero-live audit digest and never selects
``events.jsonl``, ``status.json``, ``.runtime/``, or ``.staging/``.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Iterator, Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel
from pydantic import Field, model_validator

from scripts.phase6_legacy_activity_audit import (
    CLOSED_DIRECTORY_NAME,
    CLOSED_FILE_NAMES,
    FORBIDDEN_NAMES,
    ActivityAuditV1,
    OsProcessProbe,
    ProcessProbe,
    ROOT,
    activity_audit_digest,
    audit_from_handoff,
    audit_projects,
    iter_change_roots,
)

_SHA256 = r"^[0-9a-f]{64}$"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)


class LegacyCleanupError(Exception):
    """Raised when cleanup cannot safely delete the closed legacy set."""


class LegacyCleanupEntryV1(FrozenModel):
    relative_path: str
    kind: Literal["file", "directory"]
    size: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=_SHA256)

    @model_validator(mode="after")
    def _closed_entry(self) -> LegacyCleanupEntryV1:
        parts = self.relative_path.split("/")
        if (
            len(parts) != 4
            or parts[0] != "qa"
            or parts[1] != "changes"
            or parts[2] in {"", ".", ".."}
            or parts[3] in FORBIDDEN_NAMES
        ):
            raise ValueError("entry is not an exact-depth closed path")
        if self.kind == "file":
            if parts[3] not in CLOSED_FILE_NAMES or self.sha256 is None:
                raise ValueError("file entry requires a closed name and digest")
        elif parts[3] != CLOSED_DIRECTORY_NAME or self.sha256 is not None:
            raise ValueError("directory entry must be .graph-runtime without digest")
        return self


class LegacyCleanupReportV1(FrozenModel):
    schema_version: Literal["1"]
    audit_digest: str = Field(pattern=_SHA256)
    project_roots: tuple[str, ...]
    removed: tuple[LegacyCleanupEntryV1, ...]
    runtime_directories_existed: tuple[str, ...]
    status: Literal["completed"]
    digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _sorted_unique(self) -> LegacyCleanupReportV1:
        removed_paths = tuple(item.relative_path for item in self.removed)
        if removed_paths != tuple(sorted(set(removed_paths))):
            raise ValueError("removed entries must be sorted and unique")
        if self.runtime_directories_existed != tuple(sorted(set(self.runtime_directories_existed))):
            raise ValueError("runtime directories must be sorted and unique")
        return self


def legacy_cleanup_digest(report: LegacyCleanupReportV1) -> str:
    payload = report.model_dump(mode="json")
    payload.pop("digest")
    return canonical_digest(cast(JSONValue, payload))


def _require_same_device(st: os.stat_result, expected_dev: int) -> None:
    if st.st_dev != expected_dev:
        raise LegacyCleanupError("mount crossing or different device")


def _sha256_at(dir_fd: int, name: str, size: int) -> str:
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        hasher = hashlib.sha256()
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
        return hasher.hexdigest()
    finally:
        os.close(fd)


def _inspect_named(project: Path, change: Path, change_fd: int, name: str) -> LegacyCleanupEntryV1 | None:
    if name in FORBIDDEN_NAMES:
        return None
    try:
        st = os.stat(name, dir_fd=change_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LegacyCleanupError(f"unsafe selected entry: {error}") from error
    _require_same_device(st, os.lstat(project).st_dev)
    relative = f"{change.relative_to(project).as_posix()}/{name}"
    if stat.S_ISLNK(st.st_mode):
        raise LegacyCleanupError("unsafe selected entry: symlink")
    if name == CLOSED_DIRECTORY_NAME:
        if not stat.S_ISDIR(st.st_mode):
            raise LegacyCleanupError("unsafe selected entry: runtime directory")
        return LegacyCleanupEntryV1(relative_path=relative, kind="directory", size=st.st_size, sha256=None)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        raise LegacyCleanupError("unsafe selected entry")
    digest = _sha256_at(change_fd, name, st.st_size)
    return LegacyCleanupEntryV1(relative_path=relative, kind="file", size=st.st_size, sha256=digest)


def inspect_legacy_runtime_state(project_dir: Path) -> tuple[LegacyCleanupEntryV1, ...]:
    project = Path(project_dir)
    entries: list[LegacyCleanupEntryV1] = []
    for change in iter_change_roots(project):
        change_fd = os.open(change, _DIR_FLAGS)
        try:
            for name in CLOSED_FILE_NAMES:
                entry = _inspect_named(project, change, change_fd, name)
                if entry is not None:
                    entries.append(entry)
            directory = _inspect_named(project, change, change_fd, CLOSED_DIRECTORY_NAME)
            if directory is not None:
                entries.append(directory)
        finally:
            os.close(change_fd)
    return tuple(sorted(entries, key=lambda item: item.relative_path))


@contextmanager
def _open_change_directory(project: Path, change_relative: str) -> Iterator[int]:
    parts = change_relative.split("/")
    if parts[:2] != ["qa", "changes"] or len(parts) != 3 or parts[2] in {"", ".", ".."}:
        raise LegacyCleanupError("change path is not exact-depth")
    fd = os.open(project, _DIR_FLAGS)
    try:
        expected_dev = os.fstat(fd).st_dev
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _require_same_device(os.fstat(fd), expected_dev)
        yield fd
    finally:
        os.close(fd)


def _delete_tree_nofollow(parent_fd: int, name: str, expected_dev: int) -> None:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise LegacyCleanupError(f"cannot open directory: {error}") from error
    try:
        st = os.fstat(fd)
        _require_same_device(st, expected_dev)
        if not stat.S_ISDIR(st.st_mode):
            raise LegacyCleanupError("unsafe selected entry")
        for child in sorted(os.listdir(fd)):
            child_st = os.stat(child, dir_fd=fd, follow_symlinks=False)
            _require_same_device(child_st, expected_dev)
            if stat.S_ISDIR(child_st.st_mode) and not stat.S_ISLNK(child_st.st_mode):
                _delete_tree_nofollow(fd, child, expected_dev)
                continue
            if stat.S_ISREG(child_st.st_mode) and child_st.st_nlink == 1:
                _unlink(child, fd)
                continue
            if stat.S_ISLNK(child_st.st_mode):
                _unlink(child, fd)
                continue
            raise LegacyCleanupError("special file inside runtime directory")
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise LegacyCleanupError(f"rmdir failed: {error}") from error


def _unlink(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise LegacyCleanupError(f"unlink failed: {error}") from error


def _delete_entry(change_fd: int, entry: LegacyCleanupEntryV1, expected_dev: int) -> None:
    name = Path(entry.relative_path).name
    if name in FORBIDDEN_NAMES:
        raise LegacyCleanupError("refusing to delete a Change-local result path")
    try:
        st = os.stat(name, dir_fd=change_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _require_same_device(st, expected_dev)
    if entry.kind == "file":
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise LegacyCleanupError("unsafe selected entry")
        _unlink(name, change_fd)
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise LegacyCleanupError("unsafe selected entry")
    _delete_tree_nofollow(change_fd, name, expected_dev)


def _require_zero_live_audit(audit: ActivityAuditV1, expected_digest: str) -> None:
    actual = activity_audit_digest(audit)
    if actual != expected_digest or actual != audit.digest:
        raise LegacyCleanupError("audit digest mismatch")
    if not audit.ready_for_cutover or any(
        item.activity_state in {"live", "unresolved"} for item in audit.records
    ):
        raise LegacyCleanupError("legacy activity is live or unresolved")


def cleanup_legacy_runtime_state(
    *,
    audit: ActivityAuditV1,
    expected_digest: str,
    probe: ProcessProbe | None = None,
) -> LegacyCleanupReportV1:
    _require_zero_live_audit(audit, expected_digest)
    checker = probe or OsProcessProbe()
    removed: list[LegacyCleanupEntryV1] = []
    runtime_existed: list[str] = []
    for root_text in audit.project_roots:
        project = Path(root_text)
        recheck = audit_projects((project,), probe=checker)
        if not recheck.ready_for_cutover:
            raise LegacyCleanupError("legacy activity is live or unresolved")
        inspected = inspect_legacy_runtime_state(project)
        grouped: dict[str, list[LegacyCleanupEntryV1]] = defaultdict(list)
        for entry in inspected:
            grouped["/".join(entry.relative_path.split("/")[:3])].append(entry)
        for change_relative, entries in sorted(grouped.items()):
            with _open_change_directory(project, change_relative) as change_fd:
                expected_dev = os.fstat(change_fd).st_dev
                files = [item for item in entries if item.kind == "file"]
                directories = [item for item in entries if item.kind == "directory"]
                for entry in (*sorted(files, key=lambda item: item.relative_path), *directories):
                    _delete_entry(change_fd, entry, expected_dev)
                os.fsync(change_fd)
        removed.extend(inspected)
        runtime_existed.extend(item.relative_path for item in inspected if item.kind == "directory")
    body = {
        "schema_version": "1",
        "audit_digest": audit.digest,
        "project_roots": list(audit.project_roots),
        "removed": [
            item.model_dump(mode="json") for item in sorted(removed, key=lambda item: item.relative_path)
        ],
        "runtime_directories_existed": sorted(set(runtime_existed)),
        "status": "completed",
    }
    digest = canonical_digest(cast(JSONValue, body))
    return LegacyCleanupReportV1.model_validate({**body, "digest": digest})


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove closed legacy runtime state once.")
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        live = audit_from_handoff(args.handoff, repo_root=ROOT)
        raw = json.loads(args.audit.read_text(encoding="utf-8"))
        audit = ActivityAuditV1.model_validate(raw)
        if audit.project_roots != live.project_roots:
            raise LegacyCleanupError("project roots do not match the frozen handoff")
        if activity_audit_digest(audit) != live.digest:
            raise LegacyCleanupError("audit digest mismatch")
        report = cleanup_legacy_runtime_state(audit=audit, expected_digest=args.expected_digest)
    except (LegacyCleanupError, ValueError, OSError) as error:
        print(f"legacy state cleanup rejected: {error}", file=sys.stderr)
        return 1
    _write_json(args.output, report.model_dump(mode="json"))
    print(report.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
