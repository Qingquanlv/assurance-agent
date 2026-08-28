from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

CLIENT_DISTRIBUTIONS = (
    ("agent-runtime-contracts", "agent-runtime-contracts"),
    ("agent-runtime-opencode", "agent-runtime-opencode"),
    ("agent-runtime-cursor", "agent-runtime-cursor"),
)

CLIENT_DOWNWARD_FORBIDDEN = frozenset(
    {
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
    }
)

CLIENT_SOURCE_TREES = (
    ("agent_runtime_contracts", "packages/clients/agent-runtime-contracts/agent_runtime_contracts"),
    ("agent_runtime_opencode", "packages/clients/agent-runtime-opencode/agent_runtime_opencode"),
    ("agent_runtime_cursor", "packages/clients/agent-runtime-cursor/agent_runtime_cursor"),
)

ADAPTER_PEERS = {
    "agent_runtime_opencode": "agent_runtime_cursor",
    "agent_runtime_cursor": "agent_runtime_opencode",
}


ALLOWED_ROOTS = set(sys.stdlib_module_names) | {
    "graph_engine",
    "packaging",
    "pydantic",
    "pydantic_core",
    "yaml",
}

FRAMEWORK_FORBIDDEN_ROOTS = frozenset(
    {
        "agent_runtime_contracts",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
        "graph_engine_toy_a",
        "graph_engine_toy_b",
        "agent_runtime_fixture",
    }
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_framework_has_the_only_graph_engine_source_tree(repo_root: Path) -> None:
    assert (repo_root / "packages/framework/graph-engine/graph_engine").is_dir()
    assert not (repo_root / "packages" / "graph-engine").exists()


def test_clients_have_the_only_agent_runtime_source_trees(repo_root: Path) -> None:
    for directory_name, project_name in CLIENT_DISTRIBUTIONS:
        target = repo_root / "packages" / "clients" / directory_name
        assert target.is_dir()
        assert not (repo_root / "packages" / directory_name).exists()
        pyproject = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["name"] == project_name


def test_clients_import_no_features_or_product(repo_root: Path) -> None:
    violations: list[str] = []
    for package_name, relative in CLIENT_SOURCE_TREES:
        forbidden = set(CLIENT_DOWNWARD_FORBIDDEN)
        if package_name == "agent_runtime_contracts":
            forbidden.update({"agent_runtime_opencode", "agent_runtime_cursor"})
        else:
            forbidden.add(ADAPTER_PEERS[package_name])
        root = repo_root / relative
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".", 1)[0]]
                else:
                    continue
                for name in names:
                    if name in forbidden:
                        violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []


def test_graph_engine_imports_no_product_packages(repo_root: Path) -> None:
    root = repo_root / "packages/framework/graph-engine/graph_engine"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            for name in names:
                if name in FRAMEWORK_FORBIDDEN_ROOTS or name not in ALLOWED_ROOTS:
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []
