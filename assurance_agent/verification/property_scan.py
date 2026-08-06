"""AST extraction of ``@pytest.mark.property(...)`` markers (§5-A2).

Scanners live here (not in ``evidence/``) so the evidence layer stays free of
workflow and of source-tree side effects. Unknown keys become typed
``property_unknown_key`` gaps against ``constraint_coverage``; the closed key
set is injected by the caller so Task 4's DataKnowledge keys can plug in
without this module hardcoding a SUT.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.artifacts.models.metrics import MetricCollectionGap

PROPERTY_UNKNOWN_KEY: Literal["property_unknown_key"] = "property_unknown_key"


@dataclass(frozen=True)
class PropertyMarkerHit:
    file: str
    test_name: str
    constraint_keys: tuple[str, ...]
    lineno: int
    qualified_test_name: str | None = None

    @property
    def pytest_qualified_name(self) -> str:
        """Parametrization-free pytest identity after the file segment."""
        return self.qualified_test_name or self.test_name


@dataclass(frozen=True)
class PropertyScanResult:
    hits: tuple[PropertyMarkerHit, ...]
    unknown_key_gaps: tuple[MetricCollectionGap, ...]


def extract_property_markers(source: str, *, file: str = "<string>") -> tuple[PropertyMarkerHit, ...]:
    """Return property-marker hits for every test function in ``source``.

    Discovers module-level ``test_*`` and ``class Test*: def test_*`` methods.
    """
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError:
        return ()
    hits: list[PropertyMarkerHit] = []
    for qualified_name, node in _iter_test_functions(tree):
        keys = _property_keys_from_decorators(node.decorator_list)
        if keys is None:
            continue
        hits.append(
            PropertyMarkerHit(
                file=file,
                test_name=node.name,
                constraint_keys=keys,
                lineno=node.lineno,
                qualified_test_name=qualified_name,
            )
        )
    return tuple(hits)


def _iter_test_functions(
    tree: ast.AST,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Module-level ``test_*`` plus methods under ``class Test*``."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            found.append((node.name, node))
            continue
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                    "test_"
                ):
                    found.append((f"{node.name}::{item.name}", item))
    return found


def scan_property_tests(
    paths: Iterable[Path],
    *,
    root: Path | None = None,
    known_keys: frozenset[str] | Sequence[str] | None = None,
) -> PropertyScanResult:
    """Scan source files for property markers; emit unknown-key gaps when asked.

    ``known_keys=None`` means "do not judge keys" — useful before Task 4 lands a
    closed DataKnowledge constraint vocabulary. Passing an empty frozenset means
    every key is unknown.
    """
    base = root.resolve() if root is not None else None
    known: frozenset[str] | None
    if known_keys is None:
        known = None
    else:
        known = frozenset(known_keys)

    hits: list[PropertyMarkerHit] = []
    gaps: list[MetricCollectionGap] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if base is not None:
            try:
                rel = str(path.resolve().relative_to(base))
            except ValueError:
                rel = str(path)
        else:
            rel = str(path)
        file_hits = extract_property_markers(text, file=rel)
        hits.extend(file_hits)
        if known is None:
            continue
        for hit in file_hits:
            for key in hit.constraint_keys:
                if key in known:
                    continue
                gaps.append(
                    MetricCollectionGap(
                        code=PROPERTY_UNKNOWN_KEY,
                        metric="constraint_coverage",
                        subject=key,
                        detail=(
                            f"{hit.file}::{hit.pytest_qualified_name} references "
                            f"unknown constraint key {key!r}"
                        ),
                    )
                )
    hits_sorted = tuple(sorted(hits, key=lambda h: (h.file, h.lineno, h.test_name)))
    gaps_sorted = tuple(
        sorted(gaps, key=lambda g: (g.subject, g.detail)),
    )
    return PropertyScanResult(hits=hits_sorted, unknown_key_gaps=gaps_sorted)


def _property_keys_from_decorators(decorators: list[ast.expr]) -> tuple[str, ...] | None:
    """Return constraint keys if a property marker is present, else None."""
    found = False
    keys: list[str] = []
    for decorator in decorators:
        extracted = _keys_from_decorator(decorator)
        if extracted is None:
            continue
        found = True
        keys.extend(extracted)
    if not found:
        return None
    return tuple(keys)


def _keys_from_decorator(decorator: ast.expr) -> tuple[str, ...] | None:
    if isinstance(decorator, ast.Call):
        if not _is_property_marker_attr(decorator.func):
            return None
        keys: list[str] = []
        for arg in decorator.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.append(arg.value)
        return tuple(keys)
    # Bare ``@pytest.mark.property`` (no args) is still a property marker.
    if _is_property_marker_attr(decorator):
        return ()
    return None


def _is_property_marker_attr(node: ast.expr) -> bool:
    # pytest.mark.property
    if not isinstance(node, ast.Attribute) or node.attr != "property":
        return False
    mark = node.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return False
    root = mark.value
    return isinstance(root, ast.Name) and root.id == "pytest"


__all__ = [
    "PROPERTY_UNKNOWN_KEY",
    "PropertyMarkerHit",
    "PropertyScanResult",
    "extract_property_markers",
    "scan_property_tests",
]
