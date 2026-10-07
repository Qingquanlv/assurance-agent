from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


FORBIDDEN_SYMBOLS = frozenset(
    {
        "CursorBindingV1",
        "LegacyRuntimeRecord",
        "ENTRYPOINT_RUNTIME_CUTOVER",
        "LedgerTaskActivityPort",
        "_DeferredPhase",
        "_DeferredTaskExecutor",
        "_TolerantAttemptFactory",
        "_UnusedWorkspace",
        "fixture-model",
        "negotiate_provider_schema",
    }
)

PRODUCTION_ROOTS = (
    Path("packages"),
    Path("scripts"),
)

PRODUCTION_SUFFIXES = frozenset({".py", ".sh", ".toml", ".md", ".json"})


class _ProductionRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _iter_production_files(self) -> list[Path]:
        files: list[Path] = []
        for relative in PRODUCTION_ROOTS:
            base = self.root / relative
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file() or path.suffix not in PRODUCTION_SUFFIXES:
                    continue
                if "tests" in path.parts or "__pycache__" in path.parts:
                    continue
                files.append(path)
        return files

    def production_symbol_hits(self, forbidden: set[str] | frozenset[str]) -> dict[str, list[str]]:
        hits: dict[str, list[str]] = defaultdict(list)
        for path in self._iter_production_files():
            text = path.read_text(encoding="utf-8")
            relative = str(path.relative_to(self.root))
            if path.suffix == ".py":
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                names.update(
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                )
                names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
                for symbol in forbidden:
                    if symbol in names or symbol in text:
                        hits[symbol].append(relative)
            elif any(symbol in text for symbol in forbidden):
                for symbol in forbidden:
                    if symbol in text:
                        hits[symbol].append(relative)
        return {symbol: sorted(paths) for symbol, paths in sorted(hits.items()) if paths}

    def production_pickle_imports(self) -> tuple[str, ...]:
        found: list[str] = []
        for path in self._iter_production_files():
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name.split(".", 1)[0] == "pickle" for alias in node.names):
                        found.append(str(path.relative_to(self.root)))
                elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] == "pickle":
                    found.append(str(path.relative_to(self.root)))
        return tuple(sorted(set(found)))


def test_production_tree_contains_no_deleted_runtime_surface() -> None:
    repository = _ProductionRepository(Path(__file__).resolve().parents[2])
    assert repository.production_symbol_hits(FORBIDDEN_SYMBOLS) == {}
    assert repository.production_pickle_imports() == ()
