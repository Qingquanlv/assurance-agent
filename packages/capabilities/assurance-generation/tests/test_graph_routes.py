"""Generation no longer hand-routes lanes. The Flow declares the branches."""

from __future__ import annotations

import ast
from pathlib import Path

_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation" / "graphs"


def test_generation_send_is_only_in_route_families_and_has_no_fanout_shim() -> None:
    send_hits: list[str] = []
    shim_hits: list[str] = []
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Send":
                send_hits.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in {"fanout", "min_matches", "route_families"}:
                shim_hits.append(f"{path.name}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr == "fanout":
                shim_hits.append(f"{path.name}:{node.lineno}:{node.attr}")
    assert send_hits == []
    assert shim_hits == []
    source = (_GRAPHS_ROOT / "factory.py").read_text(encoding="utf-8")
    assert "flow.parallel" in source
    assert "budget=_REVIEW_BUDGET" in source
    assert "_REVIEW_BUDGET = 3" in source
