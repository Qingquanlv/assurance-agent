"""Shared project fixtures for tests that resolve Change paths via config."""

from pathlib import Path

from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def write_aa_config(project_root: Path) -> None:
    """Write a valid `.aa/config.yaml` (required by resolve_change)."""
    aa = project_root / ".aa"
    aa.mkdir(parents=True, exist_ok=True)
    path = aa / "config.yaml"
    if not path.is_file():
        path.write_text(build_config_yaml(InitAnswers()), encoding="utf-8")
