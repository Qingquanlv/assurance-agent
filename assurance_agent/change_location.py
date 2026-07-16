"""Resolve Change directories from project config (qa.changes / qa.archive)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.config import AaConfig, load_config
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe

ChangeSource = Literal["changes", "archive"]


class ChangeNotFoundError(AaError):
    pass


class ChangeAmbiguousError(AaError):
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


def resolve_change(project_root: Path, change_id: str) -> ChangeLocation:
    """Resolve an active (in-progress) Change under qa.changes. Write-path entry."""
    assert_change_id_safe(change_id)
    config = load_config(project_root)
    changes_path = _root_for(project_root, config, "changes") / change_id
    if changes_path.is_dir():
        return ChangeLocation(
            project_root=project_root,
            change_id=change_id,
            path=changes_path,
            source="changes",
        )
    archive_path = _root_for(project_root, config, "archive") / change_id
    if archive_path.is_dir():
        raise ChangeNotFoundError(
            f"change '{change_id}' not found under active changes "
            f"(expected: {changes_path}); found under archive at {archive_path} — "
            f"write commands require an active change"
        )
    raise ChangeNotFoundError(f"change '{change_id}' not found (expected: {changes_path})")


def resolve_change_any(project_root: Path, change_id: str) -> ChangeLocation:
    """Resolve a Change under qa.changes or qa.archive. Fails if both exist."""
    assert_change_id_safe(change_id)
    config = load_config(project_root)
    changes_path = _root_for(project_root, config, "changes") / change_id
    archive_path = _root_for(project_root, config, "archive") / change_id
    in_changes = changes_path.is_dir()
    in_archive = archive_path.is_dir()
    if in_changes and in_archive:
        raise ChangeAmbiguousError(
            f"change '{change_id}' exists under both changes ({changes_path}) "
            f"and archive ({archive_path}); remove one before continuing"
        )
    if in_changes:
        return ChangeLocation(
            project_root=project_root,
            change_id=change_id,
            path=changes_path,
            source="changes",
        )
    if in_archive:
        return ChangeLocation(
            project_root=project_root,
            change_id=change_id,
            path=archive_path,
            source="archive",
        )
    raise ChangeNotFoundError(
        f"change '{change_id}' not found under changes ({changes_path}) or archive ({archive_path})"
    )
