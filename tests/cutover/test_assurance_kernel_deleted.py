from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from tests.cutover.conformance import (
    KERNEL_DESTINATION_ROOTS,
    KERNEL_DESTINATIONS,
    KERNEL_PACKAGE_PREFIX,
    kernel_deletion_paths,
    kernel_path_owner,
    live_kernel_paths,
    production_runtime_files,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_assurance_kernel_source_and_metadata_are_absent(repo_root: Path) -> None:
    assert not (repo_root / "packages" / "assurance-kernel").exists()
    runtime_text = [path.read_text(errors="ignore") for path in production_runtime_files(repo_root)]
    assert not any("assurance_kernel" in text for text in runtime_text)
    assert not any("assurance-kernel" in text for text in runtime_text)
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    dumped = json.dumps(config)
    assert "assurance_kernel" not in dumped
    assert "assurance-kernel" not in dumped
    assert KERNEL_PACKAGE_PREFIX.rstrip("/") not in dumped


def test_assurance_kernel_is_not_importable() -> None:
    assert importlib.util.find_spec("assurance_kernel") is None


def test_deleted_kernel_inventory_has_replacement_or_obsolete(repo_root: Path) -> None:
    deleted = kernel_deletion_paths(repo_root)
    assert deleted
    assert all(path.startswith(KERNEL_PACKAGE_PREFIX) for path in deleted)
    live = live_kernel_paths(repo_root)
    for path in (*deleted, *live):
        owner = kernel_path_owner(path)
        assert owner in KERNEL_DESTINATIONS, path
        if owner == "obsolete":
            continue
        destination = repo_root / KERNEL_DESTINATION_ROOTS[owner]
        assert destination.is_dir(), owner
