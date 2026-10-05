"""Scan graph modules for parsing, filesystem, and domain imports."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_IMPORT_KINDS = {
    "hashlib": "import:hashlib",
    "json": "import:json",
    "pathlib": "import:pathlib",
    "yaml": "import:yaml",
}
_CALL_ATTRS = {
    "read_bytes": "call:read_bytes",
    "read_text": "call:read_text",
    "write_bytes": "call:write_bytes",
    "write_text": "call:write_text",
    "model_validate": "call:model_validate",
    "model_validate_json": "call:model_validate",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def violation_counts(tree: ast.AST) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                kind = _import_kind(alias.name)
                if kind is not None:
                    counts[kind] += 1
        elif isinstance(node, ast.ImportFrom):
            kind = _import_kind(node.module)
            if kind is not None:
                counts[kind] += 1
        elif isinstance(node, ast.Call):
            kind = _call_kind(node)
            if kind is not None:
                counts[kind] += 1
    return {kind: counts[kind] for kind in sorted(counts)}


def scan_graph_purity(root: Path | None = None) -> dict[str, dict[str, int]]:
    base = repo_root() if root is None else root
    found: dict[str, dict[str, int]] = {}
    for path in _graph_files(base):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            raise AssertionError(f"{path.as_posix()} did not parse: {error}") from error
        counts = violation_counts(tree)
        if counts:
            found[path.relative_to(base).as_posix()] = counts
    return found


def _import_kind(module: str | None) -> str | None:
    if not module:
        return None
    top = module.split(".", 1)[0]
    if top in _IMPORT_KINDS:
        return _IMPORT_KINDS[top]
    parts = module.split(".")
    if "domain" not in parts:
        return None
    domain_at = parts.index("domain")
    if domain_at > 0 and parts[domain_at - 1].startswith("assurance_"):
        return "import:domain"
    return None


def _call_kind(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        return "call:open"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "builtins":
        if func.attr == "open":
            return "call:open"
    if isinstance(func, ast.Attribute):
        return _CALL_ATTRS.get(func.attr)
    return None


def _graph_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    capabilities = root / "packages" / "capabilities"
    if capabilities.is_dir():
        for package in sorted(path for path in capabilities.iterdir() if path.is_dir()):
            paths.extend(sorted(package.glob("assurance_*/graphs/*.py")))
    product = root / "packages" / "products" / "assurance-product" / "assurance_product" / "graphs"
    if product.is_dir():
        paths.extend(sorted(product.glob("*.py")))
    return paths
