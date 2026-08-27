"""Read-only one-shot audit of closed legacy runtime state.

Never imported by ``assurance_product`` and never called by ``aa start``/``run``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Literal, Protocol, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel
from pydantic import Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
CLOSED_FILE_NAMES = (
    ".progression.lock",
    "driver.json",
    "driver.lock",
    "running-tasks.json",
    "workflow-state.json",
    "workflow-state.yaml",
)
CLOSED_DIRECTORY_NAME = ".graph-runtime"
FORBIDDEN_NAMES = frozenset({"events.jsonl", "status.json", ".runtime", ".staging"})
PID_FILE_NAMES = frozenset({"driver.json", "running-tasks.json"})
_SHA256 = r"^[0-9a-f]{64}$"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
ActivityState = Literal["inactive", "dead", "live", "unresolved"]
EXIT_BLOCKED = 40


class LegacyActivityError(Exception):
    """Raised when the read-only scanner cannot classify a project root."""


class ProcessProbe(Protocol):
    def state(self, pid: int) -> Literal["dead", "live", "unresolved"]:
        raise NotImplementedError


class OsProcessProbe:
    def state(self, pid: int) -> Literal["dead", "live", "unresolved"]:
        if type(pid) is not int or pid <= 0:
            return "unresolved"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except (PermissionError, OSError):
            return "unresolved"
        return "live"


class ActivityRecordV1(FrozenModel):
    relative_path: str
    kind: Literal["file", "directory"]
    pids: tuple[int, ...]
    activity_state: ActivityState

    @model_validator(mode="after")
    def _closed_exact_depth(self) -> ActivityRecordV1:
        parts = self.relative_path.split("/")
        if (
            len(parts) != 4
            or parts[0] != "qa"
            or parts[1] != "changes"
            or parts[2] in {"", ".", ".."}
            or parts[3] in FORBIDDEN_NAMES
        ):
            raise ValueError("record is not an exact-depth closed path")
        if self.kind == "file" and parts[3] not in CLOSED_FILE_NAMES:
            raise ValueError("record is not a closed file")
        if self.kind == "directory" and parts[3] != CLOSED_DIRECTORY_NAME:
            raise ValueError("record is not the closed runtime directory")
        return self


class ActivityAuditV1(FrozenModel):
    schema_version: Literal["1"]
    handoff_path: str
    handoff_digest: str = Field(pattern=_SHA256)
    project_roots: tuple[str, ...]
    records: tuple[ActivityRecordV1, ...]
    ready_for_cutover: bool
    digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _ready_matches_records(self) -> ActivityAuditV1:
        paths = tuple(item.relative_path for item in self.records)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("records must be sorted and unique")
        blocked = any(item.activity_state in {"live", "unresolved"} for item in self.records)
        if self.ready_for_cutover == blocked:
            raise ValueError("ready_for_cutover must be the zero-live complement")
        return self


def activity_audit_digest(audit: ActivityAuditV1) -> str:
    payload = audit.model_dump(mode="json")
    payload.pop("digest")
    return canonical_digest(cast(JSONValue, payload))


def _require_safe_project(project: Path) -> Path:
    if not project.is_absolute():
        raise LegacyActivityError("project root must be absolute")
    try:
        st = os.lstat(project)
    except OSError as error:
        raise LegacyActivityError("project root is not a directory") from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise LegacyActivityError("project root must be a real directory")
    if project.as_posix() in {"/", "/Users", "/home", "/tmp", "/var", "/etc"} or len(project.parts) < 3:
        raise LegacyActivityError("project root is too broad")
    return project


def _lstat_dir(path: Path, expected_dev: int) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_dev != expected_dev:
        return None
    return st


def iter_change_roots(project: Path) -> tuple[Path, ...]:
    project = _require_safe_project(project)
    project_st = os.lstat(project)
    qa_st = _lstat_dir(project / "qa", project_st.st_dev)
    if qa_st is None:
        return ()
    changes_st = _lstat_dir(project / "qa" / "changes", project_st.st_dev)
    if changes_st is None:
        return ()
    roots: list[Path] = []
    with os.scandir(project / "qa" / "changes") as entries:
        children = sorted(entries, key=lambda item: item.name)
    for entry in children:
        if entry.name in {"", ".", ".."} or "/" in entry.name or "\\" in entry.name:
            continue
        child = _lstat_dir(Path(entry.path), project_st.st_dev)
        if child is None:
            continue
        roots.append(Path(entry.path))
    return tuple(roots)


def _read_json_nofollow(path: Path) -> object | None:
    try:
        fd = os.open(path, _FILE_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        payload = b""
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            payload += chunk
            remaining -= len(chunk)
    finally:
        os.close(fd)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _pids_from_driver(payload: object, change_dir: Path) -> tuple[int, ...] | None:
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    directory = payload.get("directory")
    if type(pid) is not int or pid <= 0 or not isinstance(directory, str):
        return None
    expected = {str(change_dir), os.path.realpath(change_dir)}
    if directory not in expected:
        return None
    return (pid,)


def _pids_from_running_tasks(payload: object) -> tuple[int, ...] | None:
    if not isinstance(payload, dict):
        return None
    leases = payload.get("leases")
    if not isinstance(leases, list):
        return None
    pids: list[int] = []
    for lease in leases:
        if not isinstance(lease, dict):
            return None
        pid = lease.get("pid")
        if type(pid) is not int or pid <= 0:
            return None
        pids.append(pid)
    return tuple(pids)


def _combine_states(states: tuple[ActivityState, ...]) -> ActivityState:
    if "live" in states:
        return "live"
    if "unresolved" in states:
        return "unresolved"
    if "dead" in states:
        return "dead"
    return "inactive"


def _classify_pid_file(
    path: Path, change_dir: Path, probe: ProcessProbe
) -> tuple[tuple[int, ...], ActivityState]:
    payload = _read_json_nofollow(path)
    if payload is None:
        return (), "unresolved"
    extracted = (
        _pids_from_driver(payload, change_dir)
        if path.name == "driver.json"
        else _pids_from_running_tasks(payload)
    )
    if extracted is None:
        return (), "unresolved"
    return extracted, _combine_states(tuple(probe.state(pid) for pid in extracted)) or "inactive"


def _record_closed_entry(
    project: Path, change_dir: Path, name: str, kind: Literal["file", "directory"], probe: ProcessProbe
) -> ActivityRecordV1 | None:
    if name in FORBIDDEN_NAMES:
        return None
    path = change_dir / name
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return None
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        return None
    if kind == "directory" and not stat.S_ISDIR(st.st_mode):
        return None
    relative = f"{change_dir.relative_to(project).as_posix()}/{name}"
    pids: tuple[int, ...] = ()
    activity: ActivityState = "inactive"
    if name in PID_FILE_NAMES:
        pids, activity = _classify_pid_file(path, change_dir, probe)
    return ActivityRecordV1(relative_path=relative, kind=kind, pids=pids, activity_state=activity)


def audit_projects(
    projects: tuple[Path, ...],
    probe: ProcessProbe,
    *,
    handoff_path: str = "",
    handoff_digest: str | None = None,
) -> ActivityAuditV1:
    records: list[ActivityRecordV1] = []
    resolved_roots: list[str] = []
    for project in projects:
        root = _require_safe_project(project)
        resolved_roots.append(str(root.resolve()))
        for change in iter_change_roots(root):
            for name in CLOSED_FILE_NAMES:
                record = _record_closed_entry(root, change, name, "file", probe)
                if record is not None:
                    records.append(record)
            directory = _record_closed_entry(root, change, CLOSED_DIRECTORY_NAME, "directory", probe)
            if directory is not None:
                records.append(directory)
    ordered = tuple(sorted(records, key=lambda item: item.relative_path))
    ready = not any(item.activity_state in {"live", "unresolved"} for item in ordered)
    roots = tuple(resolved_roots)
    digest_source = handoff_digest or canonical_digest(cast(JSONValue, {"project_roots": list(roots)}))
    body = {
        "schema_version": "1",
        "handoff_path": handoff_path,
        "handoff_digest": digest_source,
        "project_roots": list(roots),
        "records": [item.model_dump(mode="json") for item in ordered],
        "ready_for_cutover": ready,
    }
    digest = canonical_digest(cast(JSONValue, body))
    return ActivityAuditV1.model_validate({**body, "digest": digest})


def audit_from_handoff(
    handoff_path: Path,
    *,
    repo_root: Path,
    probe: ProcessProbe | None = None,
) -> ActivityAuditV1:
    from tests.phase6.conformance import handoff_canonical_digest, parse_phase5_handoff

    parsed = parse_phase5_handoff(handoff_path, repo_root=repo_root)
    resolved = handoff_path if handoff_path.is_absolute() else repo_root / handoff_path
    relative = Path(os.path.relpath(resolved.resolve(), repo_root.resolve())).as_posix()
    roots = tuple(Path(item) for item in parsed.project_roots)
    return audit_projects(
        roots,
        probe=probe or OsProcessProbe(),
        handoff_path=relative,
        handoff_digest=handoff_canonical_digest(parsed),
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit closed legacy runtime activity once.")
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        audit = audit_from_handoff(args.handoff, repo_root=ROOT)
    except (LegacyActivityError, ValueError) as error:
        print(f"legacy activity audit rejected: {error}", file=sys.stderr)
        return 1
    _write_json(args.output, audit.model_dump(mode="json"))
    print(audit.digest)
    if not audit.ready_for_cutover:
        return EXIT_BLOCKED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
