"""Resolve Change directories from project config (qa.changes / qa.archive)."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from assurance_kernel.config import (
    CONFIG_RELPATH,
    AaConfig,
    ConfigInvalidError,
    ConfigNotFoundError,
    load_config,
)
from assurance_kernel.exceptions import AaError
from assurance_kernel.identifiers import assert_change_id_safe

ChangeSource = Literal["changes", "archive"]

# Role preference for resolution:
#   "active"  → the writeable in-progress Change under qa.changes;
#   "archive" → the archived (final-snapshot) Change under qa.archive.
# `aa-archive` copies (never moves) and does not delete qa/changes/<id>, so a
# Change existing under both roots is the normal steady state, not an anomaly
# (see ADR-0002). Resolution therefore picks by preference; it never treats
# coexistence as an error.
ChangePreference = Literal["active", "archive"]
ChangeLstatKind = Literal["missing", "directory", "symlink", "other"]


class ChangeNotFoundError(AaError):
    pass


class ChangeLocationError(AaError):
    """Unsafe or invalid change-location configuration/probe."""


@dataclass(frozen=True)
class ChangeLocation:
    project_root: Path
    change_id: str
    path: Path
    source: ChangeSource


@dataclass(frozen=True)
class ParsedChangeRoots:
    changes_root: str
    archive_root: str


@dataclass(frozen=True)
class ChangeLocationProbe:
    source: ChangeSource
    configured_root: str
    candidate_repo_path: str
    lstat_kind: ChangeLstatKind
    mode: int | None


@dataclass(frozen=True)
class DecidedChangeLocation:
    change_id: str
    source: ChangeSource
    repo_path: str
    configured_root: str


def _normalize_rel(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


def _validate_configured_root(rel: str, *, label: str) -> str:
    if not isinstance(rel, str) or not rel.strip():
        raise ChangeLocationError(f"{label} must be a non-empty relative path")
    normalized = _normalize_rel(rel.strip().replace("\\", "/"))
    if normalized.startswith("/") or normalized.startswith("~"):
        raise ChangeLocationError(f"{label} must be repository-relative (got absolute): {rel}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ChangeLocationError(f"{label} must not contain '..': {rel}")
    if not parts:
        raise ChangeLocationError(f"{label} must be a non-empty relative path")
    return "/".join(parts)


def parse_change_roots(config_bytes: bytes) -> ParsedChangeRoots:
    """Parse configured changes/archive roots from exact config bytes."""
    try:
        raw = yaml.safe_load(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as err:
        raise ConfigInvalidError(f"{CONFIG_RELPATH} parse error: {err}") from err
    try:
        config = AaConfig.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigInvalidError(f"{CONFIG_RELPATH} schema invalid: {loc}: {first['msg']}") from err
    return ParsedChangeRoots(
        changes_root=_validate_configured_root(config.qa.changes, label="qa.changes"),
        archive_root=_validate_configured_root(config.qa.archive, label="qa.archive"),
    )


def _candidate_repo_path(configured_root: str, change_id: str) -> str:
    return f"{configured_root.rstrip('/')}/{change_id}"


def _assert_repo_relative(project_root: Path, repo_path: str) -> Path:
    """Return the joined candidate path after a non-following containment check."""
    root = project_root if project_root.is_absolute() else project_root.absolute()
    # Walk components without resolving symlinks so a symlinked leaf remains a leaf.
    cursor = root
    for part in repo_path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ChangeLocationError(f"change candidate escapes repository: {repo_path}")
        cursor = cursor / part
        try:
            cursor.relative_to(root)
        except ValueError as exc:
            raise ChangeLocationError(f"change candidate escapes repository: {repo_path}") from exc
    return cursor


def _probe_one(
    project_root: Path,
    *,
    source: ChangeSource,
    configured_root: str,
    change_id: str,
) -> ChangeLocationProbe:
    repo_path = _candidate_repo_path(configured_root, change_id)
    absolute = _assert_repo_relative(project_root, repo_path)

    try:
        st = os.lstat(absolute)
    except FileNotFoundError:
        return ChangeLocationProbe(
            source=source,
            configured_root=configured_root,
            candidate_repo_path=repo_path,
            lstat_kind="missing",
            mode=None,
        )
    except OSError as exc:
        raise ChangeLocationError(f"failed to lstat change candidate {repo_path}: {exc}") from exc

    mode = int(st.st_mode)
    if stat.S_ISLNK(mode):
        kind: ChangeLstatKind = "symlink"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    else:
        kind = "other"
    return ChangeLocationProbe(
        source=source,
        configured_root=configured_root,
        candidate_repo_path=repo_path,
        lstat_kind=kind,
        mode=mode,
    )


def probe_change_location_candidates(
    project_root: Path,
    change_id: str,
    roots: ParsedChangeRoots,
) -> tuple[ChangeLocationProbe, ChangeLocationProbe]:
    """Containment-safe lstat probes for changes then archive candidates."""
    assert_change_id_safe(change_id)
    changes = _probe_one(
        project_root,
        source="changes",
        configured_root=roots.changes_root,
        change_id=change_id,
    )
    archive = _probe_one(
        project_root,
        source="archive",
        configured_root=roots.archive_root,
        change_id=change_id,
    )
    return changes, archive


def decide_change_location(
    *,
    change_id: str,
    preference: ChangePreference,
    roots: ParsedChangeRoots,
    probes: tuple[ChangeLocationProbe, ChangeLocationProbe] | Sequence[ChangeLocationProbe],
) -> DecidedChangeLocation:
    """Decide change location from parsed roots and candidate probes.

    Only a real directory candidate is selectable. Symlinks and other kinds are
    treated as absent for write-path resolution (never followed).
    """
    assert_change_id_safe(change_id)
    probe_list = tuple(probes)
    if len(probe_list) != 2 or [p.source for p in probe_list] != ["changes", "archive"]:
        raise ChangeLocationError("probes must be exactly [changes, archive]")
    changes_probe, archive_probe = probe_list
    if changes_probe.configured_root != roots.changes_root:
        raise ChangeLocationError("changes probe configured_root mismatch")
    if archive_probe.configured_root != roots.archive_root:
        raise ChangeLocationError("archive probe configured_root mismatch")

    in_changes = changes_probe.lstat_kind == "directory"
    in_archive = archive_probe.lstat_kind == "directory"
    changes_path = changes_probe.candidate_repo_path
    archive_path = archive_probe.candidate_repo_path

    if preference == "archive":
        if in_archive:
            return DecidedChangeLocation(
                change_id=change_id,
                source="archive",
                repo_path=archive_path,
                configured_root=roots.archive_root,
            )
        if in_changes:
            return DecidedChangeLocation(
                change_id=change_id,
                source="changes",
                repo_path=changes_path,
                configured_root=roots.changes_root,
            )
        raise ChangeNotFoundError(
            f"change '{change_id}' not found under archive ({archive_path}) or changes ({changes_path})"
        )

    if in_changes:
        return DecidedChangeLocation(
            change_id=change_id,
            source="changes",
            repo_path=changes_path,
            configured_root=roots.changes_root,
        )
    if in_archive:
        raise ChangeNotFoundError(
            f"change '{change_id}' not found under active changes "
            f"(expected: {changes_path}); found under archive at {archive_path} — "
            f"write commands require an active change"
        )
    raise ChangeNotFoundError(f"change '{change_id}' not found (expected: {changes_path})")


def _root_for(project_root: Path, config: AaConfig, source: ChangeSource) -> Path:
    rel = config.qa.changes if source == "changes" else config.qa.archive
    return project_root / _normalize_rel(rel)


def changes_root(project_root: Path) -> Path:
    """Absolute qa.changes root (config-relative, './'-normalized)."""
    return _root_for(project_root, load_config(project_root), "changes")


def archive_root(project_root: Path) -> Path:
    """Absolute qa.archive root (config-relative, './'-normalized)."""
    return _root_for(project_root, load_config(project_root), "archive")


def resolve_change(
    project_root: Path,
    change_id: str,
    *,
    prefer: ChangePreference = "active",
) -> ChangeLocation:
    """Resolve a Change directory by role preference.

    - ``prefer="active"`` (write-path default): return the in-progress Change
      under qa.changes; if only an archived copy exists, raise
      ``ChangeNotFoundError`` (write commands require an active change).
    - ``prefer="archive"``: return the archived Change under qa.archive if
      present, else fall back to the active copy under qa.changes.

    Coexistence under both roots is the normal post-archive steady state and is
    never an error (ADR-0002).
    """
    assert_change_id_safe(change_id)
    config_path = project_root / CONFIG_RELPATH
    if not config_path.is_file():
        raise ConfigNotFoundError(f"{CONFIG_RELPATH} not found. Run `aa init` first.")
    config_bytes = config_path.read_bytes()
    roots = parse_change_roots(config_bytes)
    probes = probe_change_location_candidates(project_root, change_id, roots)
    decided = decide_change_location(
        change_id=change_id,
        preference=prefer,
        roots=roots,
        probes=probes,
    )
    return ChangeLocation(
        project_root=project_root,
        change_id=change_id,
        path=project_root / decided.repo_path,
        source=decided.source,
    )
