"""The only control outputs are the attempt-projection fields that have no file yet."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_ROOTS = (
    ROOT / "packages/capabilities",
    ROOT / "packages/products/assurance-product",
)
_EXPECTED = {
    "packages/capabilities/assurance-intake/assurance_intake/graphs/prepare.py": {
        "plan_digest": "plan.plan_digest",
        "selected_test_families": "plan.selected_test_families",
        "preparation_refs": "preparation_refs",
    },
    "packages/capabilities/assurance-intake/assurance_intake/graphs/case.py": {
        "reviewed_refs": "reviewed_case.preparation_refs",
    },
}


def _controls(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "control":
            continue
        for keyword in node.keywords:
            if keyword.arg is None or not isinstance(keyword.value, ast.Constant):
                raise AssertionError(f"{path} control output is not a literal path")
            if not isinstance(keyword.value.value, str):
                raise AssertionError(f"{path} control output is not a literal path")
            found[keyword.arg] = keyword.value.value
    return found


def test_control_outputs_are_only_the_projection_fields() -> None:
    found: dict[str, dict[str, str]] = {}
    for root in _ROOTS:
        for path in root.rglob("*.py"):
            controls = _controls(path)
            if controls:
                found[str(path.relative_to(ROOT))] = controls
    assert found == _EXPECTED
