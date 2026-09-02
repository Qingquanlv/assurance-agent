from __future__ import annotations

import ast
from pathlib import Path

PHASE4_WORKFLOW = Path(
    "tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/workflow.yaml"
)
CONVERTED_PRODUCTS = (
    Path("examples/graph-engine-toy-a/graph_engine_toy_a/product.py"),
    Path("examples/graph-engine-toy-b/graph_engine_toy_b/product.py"),
    Path("examples/agent-runtime-fixture/agent_runtime_fixture/product.py"),
    Path("tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product.py"),
)
FORBIDDEN_CONVERSION_NAMES = frozenset(
    {
        "WorkflowDef",
        "parse_workflow",
        "graph_engine.graph.schema",
    }
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_phase4_six_wheel_workflow_yaml_is_deleted() -> None:
    assert not (_repo_root() / PHASE4_WORKFLOW).exists()


def _product_uses_workflow_def(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "graph_engine.graph.schema" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "graph_engine.graph.schema":
                return True
            if any(alias.name in FORBIDDEN_CONVERSION_NAMES for alias in node.names):
                return True
    return False


def test_converted_examples_and_phase4_fixture_do_not_embed_workflow_def() -> None:
    still_embedded = tuple(
        path.as_posix()
        for path in CONVERTED_PRODUCTS
        if _product_uses_workflow_def(_repo_root() / path)
    )
    assert still_embedded == ()
