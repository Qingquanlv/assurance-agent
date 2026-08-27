from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

import yaml

from tests.phase4.ownership import OWNERSHIP_PATH, legacy_hook_fields, load_ownership_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_WHEEL_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "packages" / "assurance-intake",
    REPO_ROOT / "packages" / "assurance-generation",
    REPO_ROOT / "packages" / "assurance-execution",
    REPO_ROOT / "packages" / "assurance-healing",
    REPO_ROOT / "packages" / "assurance-quality",
    REPO_ROOT / "packages" / "assurance-improvement",
)
CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "product-hooks-cases.yaml"
_FORBIDDEN_TYPE_NAMES = frozenset({"Hooks", "HookRegistry", "ProductRuntime", "SemanticPins"})
_REGISTRY_KINDS = frozenset(
    {
        "TaskHandler",
        "CommitValidator",
        "DurableEffectHandler",
        "EffectRegistration",
    }
)


def forbidden_symbol_scan(roots: Sequence[Path], symbols: Sequence[str]) -> set[str]:
    wanted = frozenset(symbols)
    found: set[str] = set()
    for root in roots:
        package = root / root.name.replace("-", "_")
        for path in package.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in wanted:
                    found.add(node.id)
                elif isinstance(node, ast.Attribute) and node.attr in wanted:
                    found.add(node.attr)
                elif isinstance(node, ast.alias) and (node.name in wanted or node.asname in wanted):
                    found.add(node.name if node.name in wanted else str(node.asname))
    return found


def load_hook_cases() -> tuple[dict[str, object], ...]:
    raw = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "1":
        raise AssertionError("product-hooks-cases.yaml must use schema_version 1")
    rows = raw.get("hooks")
    if not isinstance(rows, list):
        raise AssertionError("product-hooks-cases.yaml must contain a hooks list")
    parsed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AssertionError("each hook case must be a mapping")
        parsed.append({str(key): value for key, value in row.items()})
    return tuple(parsed)


def test_every_product_hook_has_one_verified_replacement() -> None:
    legacy_fields = set(legacy_hook_fields())
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    migrate = {
        item.legacy_id for item in ledger.items if item.kind == "hook" and item.disposition == "migrate"
    }
    verified = {item.legacy_id for item in ledger.items if item.kind == "hook" and item.status == "verified"}
    pins = next(item for item in ledger.items if item.kind == "hook" and item.legacy_id == "semantic_pins")
    assert migrate == verified
    assert migrate | {pins.legacy_id} == legacy_fields
    assert pins.disposition == "delete_phase6"
    assert pins.owner is None
    assert pins.new_id is None
    assert pins.status == "planned"
    assert pins.verification is None


def test_no_new_wheel_imports_or_recreates_product_hooks() -> None:
    assert (
        forbidden_symbol_scan(
            NEW_WHEEL_ROOTS,
            symbols=("ProductHooks", "install_product_hooks", "current_product_hooks", "semantic_pins"),
        )
        == set()
    )


def test_fixture_covers_every_legacy_hook_field() -> None:
    rows = load_hook_cases()
    names = [str(row["hook"]) for row in rows]
    expected = sorted(legacy_hook_fields())
    assert names == expected or set(names) == set(expected)
    assert len(names) == len(set(names)) == 18
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    by_id = {item.legacy_id: item for item in ledger.items if item.kind == "hook"}
    for row in rows:
        item = by_id[str(row["hook"])]
        owner = row["owner"]
        new_id = row["new_id"]
        assert item.owner == owner
        assert item.new_id == new_id
        assert "compare" in row
        assert "input" in row


def test_no_hidden_catchall_hook_registry() -> None:
    assert _catchall_scan(NEW_WHEEL_ROOTS) == set()


def _catchall_scan(roots: Sequence[Path]) -> set[str]:
    hits: set[str] = set()
    for root in roots:
        package = root / root.name.replace("-", "_")
        for path in package.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if node.name in _FORBIDDEN_TYPE_NAMES:
                    hits.add(f"{path.as_posix()}:{node.name}")
                    continue
                if not _is_store_type(node):
                    continue
                kinds = _registry_kinds_in_class(node)
                if len(kinds) > 1:
                    hits.add(f"{path.as_posix()}:{node.name}")
    return hits


def _is_store_type(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        name = _expr_name(decorator)
        if name in {"dataclass", "frozen"}:
            return True
    for base in node.bases:
        if _expr_name(base) in {"Protocol", "TypedDict"}:
            return True
    return False


def _registry_kinds_in_class(node: ast.ClassDef) -> set[str]:
    kinds: set[str] = set()
    for item in node.body:
        annotation = None
        if isinstance(item, ast.AnnAssign):
            annotation = item.annotation
        elif isinstance(item, ast.FunctionDef) and item.name == "__init__":
            for arg in item.args.args[1:]:
                if arg.annotation is not None:
                    kinds.update(_annotation_kinds(arg.annotation))
        if annotation is not None:
            kinds.update(_annotation_kinds(annotation))
    return kinds


def _annotation_kinds(annotation: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(annotation):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in _REGISTRY_KINDS:
            found.add(name)
    return found


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return None
