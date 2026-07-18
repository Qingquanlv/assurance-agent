"""Resolve Change directories from project config (qa.changes / qa.archive)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.config import AaConfig, load_config
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe

ChangeSource = Literal["changes", "archive"]

# Role preference for resolution:
#   "active"  → the writeable in-progress Change under qa.changes;
#   "archive" → the archived (final-snapshot) Change under qa.archive.
# `aa-archive` copies (never moves) and does not delete qa/changes/<id>, so a
# Change existing under both roots is the normal steady state, not an anomaly
# (see ADR-0002). Resolution therefore picks by preference; it never treats
# coexistence as an error.
ChangePreference = Literal["active", "archive"]


class ChangeNotFoundError(AaError):
    pass


@dataclass(frozen=True)
class ChangeLocation:
    project_root: Path
    change_id: str
    path: Path
    source: ChangeSource


def _normalize_rel(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


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
    config = load_config(project_root)
    changes_path = _root_for(project_root, config, "changes") / change_id
    archive_path = _root_for(project_root, config, "archive") / change_id
    in_changes = changes_path.is_dir()
    in_archive = archive_path.is_dir()

    if prefer == "archive":
        if in_archive:
            return ChangeLocation(project_root, change_id, archive_path, "archive")
        if in_changes:
            return ChangeLocation(project_root, change_id, changes_path, "changes")
        raise ChangeNotFoundError(
            f"change '{change_id}' not found under archive ({archive_path}) or changes ({changes_path})"
        )

    if in_changes:
        return ChangeLocation(project_root, change_id, changes_path, "changes")
    if in_archive:
        raise ChangeNotFoundError(
            f"change '{change_id}' not found under active changes "
            f"(expected: {changes_path}); found under archive at {archive_path} — "
            f"write commands require an active change"
        )
    raise ChangeNotFoundError(f"change '{change_id}' not found (expected: {changes_path})")
