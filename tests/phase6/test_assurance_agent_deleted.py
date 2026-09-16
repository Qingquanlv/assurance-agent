from __future__ import annotations

import ast
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.phase6.conformance import (
    PHASE4_INVENTORY_RELATIVE_PATH,
    PHASE4_SDD,
    agent_deletion_paths,
    deletion_proof,
    ownership_legacy_id_for_path,
    production_runtime_files,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_assurance_agent_source_and_metadata_are_absent(repo_root: Path) -> None:
    assert not (repo_root / "assurance_agent").exists()
    assert not any(
        "assurance_agent" in path.read_text(errors="ignore") for path in production_runtime_files(repo_root)
    )
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    assert "assurance_agent" not in json.dumps(config)


def test_assurance_agent_is_not_importable() -> None:
    assert importlib.util.find_spec("assurance_agent") is None


def test_conftest_does_not_import_assurance_agent(repo_root: Path) -> None:
    source = (repo_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "assurance_agent" not in imported


def test_phase4_deletion_ledger_lives_under_test_fixtures() -> None:
    assert PHASE4_SDD.as_posix() == "tests/phase4/fixtures"
    assert ".superpowers" not in PHASE4_SDD.as_posix()


def test_deleted_agent_inventory_has_replacement_or_obsolete(repo_root: Path) -> None:
    deleted = agent_deletion_paths(repo_root)
    assert deleted
    assert (repo_root / PHASE4_INVENTORY_RELATIVE_PATH).is_file()
    ledger = yaml.safe_load((repo_root / PHASE4_SDD / "ownership.yaml").read_text(encoding="utf-8"))
    by_id = {item["legacy_id"]: item for item in ledger["items"]}
    for path in deleted:
        item = by_id.get(ownership_legacy_id_for_path(path))
        assert item is not None, path
        proof = deletion_proof(item)
        assert proof == "obsolete" or proof.startswith("tests/") or proof.startswith("packages/")
        if proof != "obsolete":
            assert (repo_root / proof).is_file(), proof
