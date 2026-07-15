"""Explore artifact paths (qa/changes/<id>/explore/). Transcribed from src/risk/paths.ts."""
from pathlib import Path

from assurance_agent.risk.safety import assert_change_id_safe, resolve_inside_project


def explore_dir(project_root: Path, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    return resolve_inside_project(project_root, "qa", "changes", change_id, "explore")


def context_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "context.json"


def advisory_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "advisory.json"
