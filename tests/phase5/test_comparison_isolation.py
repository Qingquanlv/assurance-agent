from __future__ import annotations

import ast
from pathlib import Path

HARNESS_ROOT = Path("benchmark/assurance-product-phase5")
FORBIDDEN_IMPORTS = ("assurance_agent", "graph_engine", "assurance_product")


def imported_top_level_modules(root: Path) -> set[str]:
    names: set[str] = set()
    if not root.is_dir():
        return names
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def test_comparison_modules_do_not_import_runtime_packages() -> None:
    assert (HARNESS_ROOT / "compare.py").is_file()
    assert (HARNESS_ROOT / "projection.py").is_file()
    assert (HARNESS_ROOT / "eval.py").is_file()
    assert (HARNESS_ROOT / "schemas" / "behavioral-projection-v1.json").is_file()
    imports = imported_top_level_modules(HARNESS_ROOT)
    assert "assurance_agent" not in imports
    assert "graph_engine" not in imports
    assert "assurance_product" not in imports


def test_imported_top_level_modules_scans_ast_without_importing(tmp_path: Path) -> None:
    poisoned = tmp_path / "poison.py"
    poisoned.write_text(
        "import assurance_agent\nimport graph_engine\nraise SystemExit('imported at runtime')\n",
        encoding="utf-8",
    )
    imports = imported_top_level_modules(tmp_path)
    assert "assurance_agent" in imports
    assert "graph_engine" in imports
    assert FORBIDDEN_IMPORTS[0] in imports


def test_isolation_scan_is_static_ast_walk() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(Path(__file__)))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "imported_top_level_modules"
    )
    called = {
        ast.unparse(node.func) if isinstance(node, ast.Call) else ""
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }
    assert "ast.parse" in called
    assert "importlib.import_module" not in called
    assert "__import__" not in "\n".join(called)
