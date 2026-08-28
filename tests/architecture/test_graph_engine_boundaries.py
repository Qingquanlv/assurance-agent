from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


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
