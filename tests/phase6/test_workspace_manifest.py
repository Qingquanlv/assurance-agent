from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_root_is_not_a_publishable_distribution(repo_root: Path) -> None:
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    assert config["tool"]["uv"]["package"] is False
    assert "build-system" not in config
    assert "scripts" not in config.get("project", {})
    assert "assurance_agent.products" not in config.get("project", {}).get("entry-points", {})


def test_lock_has_no_root_assurance_agent_package(repo_root: Path) -> None:
    lock = tomllib.loads((repo_root / "uv.lock").read_text())
    assert "assurance-agent" not in lock["manifest"]["members"]
    assert not any(package["name"] == "assurance-agent" for package in lock["package"])


def test_workspace_retains_kernel_until_later_tasks(repo_root: Path) -> None:
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    members = config["tool"]["uv"]["workspace"]["members"]
    assert "packages/assurance-kernel" in members
    assert not (repo_root / "assurance_agent").exists()
    assert (repo_root / "packages/assurance-kernel").is_dir()
