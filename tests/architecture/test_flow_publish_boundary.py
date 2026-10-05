"""Business flows cannot publish by reading Flow state."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLOW_PACKAGE = ROOT / "packages/framework/graph-engine/graph_engine/flow"
CAPABILITY_GRAPHS = ROOT / "packages/capabilities"


def _step_ops(tree: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "step":
            continue
        if len(node.args) >= 2:
            found.append(node.args[1])
            continue
        for keyword in node.keywords:
            if keyword.arg == "op":
                found.append(keyword.value)
    return found


def test_flow_package_has_no_publish_callback() -> None:
    import graph_engine.flow as flow

    assert "Projected" not in flow.__all__
    assert not hasattr(flow, "Projected")
    protocol = (FLOW_PACKAGE / "protocol.py").read_text(encoding="utf-8")
    compile_source = (FLOW_PACKAGE / "compile.py").read_text(encoding="utf-8")
    assert "class Projected" not in protocol
    assert "view.project" not in compile_source


def test_capability_flow_steps_are_ops_or_contracts() -> None:
    offenders: list[str] = []
    for path in CAPABILITY_GRAPHS.rglob("*.py"):
        if "graphs" not in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for op in _step_ops(tree):
            if isinstance(op, ast.Lambda | ast.Call):
                offenders.append(f"{path.relative_to(ROOT)}:{op.lineno}")
    assert offenders == []
