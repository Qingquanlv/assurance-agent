"""Explore artifact paths (<qa.changes>/<id>/explore/). Transcribed from src/risk/paths.ts."""

from pathlib import Path

from assurance_agent.change_location import changes_root
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.risk.safety import assert_change_id_safe, assert_inside_project


def explore_dir(project_root: Path, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    # Honor a configured qa.changes root; risk may also run in a bare directory
    # without .aa/config.yaml, so fall back to the documented default layout.
    try:
        base = changes_root(project_root)
    except ConfigNotFoundError:
        base = project_root / "qa" / "changes"
    target = base / change_id / "explore"
    assert_inside_project(project_root, target)
    return target


def context_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "context.json"


def advisory_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "advisory.json"
