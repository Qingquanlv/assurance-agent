from __future__ import annotations

import ast
import sys
from pathlib import Path


ALLOWED_ROOTS = set(sys.stdlib_module_names) | {
    "graph_engine",
    "packaging",
    "pydantic",
    "pydantic_core",
    "yaml",
}


def test_graph_engine_imports_no_product_packages() -> None:
    root = Path("packages/graph-engine/graph_engine")
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
                if name not in ALLOWED_ROOTS:
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []
