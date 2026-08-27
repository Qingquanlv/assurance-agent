#!/usr/bin/env python3
"""Reject leftover deleted-package seams in committed source and current docs."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DELETED = "assurance"
FORBIDDEN_IMPORT_ROOTS = {_DELETED + "_agent", _DELETED + "_kernel"}
FORBIDDEN_DISTRIBUTIONS = {_DELETED + "-agent", _DELETED + "-kernel"}
FORBIDDEN_CLI = "aa" + "-next"
FORBIDDEN_ENTRY_GROUP = _DELETED + "_agent" + ".products"
FORBIDDEN_HOOKS = "Product" + "Hooks"
FORBIDDEN_DEFAULT_GRAPH = "graph: assurance-full"
FORBIDDEN_RUNTIME_SELECTOR = "AA" + "_RUNTIME=legacy"
FORBIDDEN_DOC_TOKENS = frozenset(
    {
        *FORBIDDEN_IMPORT_ROOTS,
        *FORBIDDEN_DISTRIBUTIONS,
        FORBIDDEN_CLI,
        FORBIDDEN_ENTRY_GROUP,
        FORBIDDEN_HOOKS,
    }
)

RUNTIME_PACKAGE_ROOTS = (
    "packages/assurance-product/assurance_product",
    "packages/assurance-intake/assurance_intake",
    "packages/assurance-generation/assurance_generation",
    "packages/assurance-execution/assurance_execution",
    "packages/assurance-healing/assurance_healing",
    "packages/assurance-quality/assurance_quality",
    "packages/assurance-improvement/assurance_improvement",
    "packages/graph-engine/graph_engine",
    "packages/agent-runtime-contracts/agent_runtime_contracts",
    "packages/agent-runtime-opencode/agent_runtime_opencode",
    "packages/agent-runtime-cursor/agent_runtime_cursor",
)
RUNTIME_METADATA_FILES = (
    "pyproject.toml",
    "uv.lock",
    "packages/assurance-product/pyproject.toml",
    "packages/assurance-intake/pyproject.toml",
    "packages/assurance-generation/pyproject.toml",
    "packages/assurance-execution/pyproject.toml",
    "packages/assurance-healing/pyproject.toml",
    "packages/assurance-quality/pyproject.toml",
    "packages/assurance-improvement/pyproject.toml",
    "packages/graph-engine/pyproject.toml",
    "packages/agent-runtime-contracts/pyproject.toml",
    "packages/agent-runtime-opencode/pyproject.toml",
    "packages/agent-runtime-cursor/pyproject.toml",
)
CURRENT_DOC_FILES = (
    "README.md",
    "AGENTS.md",
    "CONTEXT.md",
    "packages/assurance-product/README.md",
    "packages/graph-engine/README.md",
)
DELETED_ROOTS = ("assurance_agent", "packages/assurance-kernel")
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    ".ruff_cache",
    ".pytest_cache",
    ".import_linter_cache",
    ".history",
    "__pycache__",
    "dist",
    "tmp",
    "node_modules",
}
DETECTION_SCRIPT_NAMES = {
    "check_no_legacy.py",
    "no_legacy_allowlist.txt",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    token: str
    reason: str = ""


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_allowed(root: Path, path: Path, allowlist: set[str]) -> bool:
    try:
        return _relative(root, path) in allowlist
    except ValueError:
        return False


def load_allowlist(path: Path, *, root: Path | None = None) -> tuple[Path, ...]:
    base = root if root is not None else path.resolve().parent.parent
    entries: list[Path] = []
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(char in line for char in "*?[]"):
            raise ValueError(f"wildcard allowlist entry is invalid: {line}")
        if line.endswith("/"):
            raise ValueError(f"directory-wide allowlist entry is invalid: {line}")
        relative = Path(line)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"allowlist entry must be an exact relative file: {line}")
        resolved = base / relative
        if resolved.is_dir():
            raise ValueError(f"directory-wide allowlist entry is invalid: {line}")
        entries.append(relative)
    return tuple(entries)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _scan_python_imports(root: Path, path: Path) -> list[Violation]:
    source = _read_text(path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [Violation(path, "syntax", "unparseable python")]
    found = sorted(_imported_roots(tree) & FORBIDDEN_IMPORT_ROOTS)
    return [Violation(path, name, "legacy import") for name in found]


def _token_hits(text: str) -> tuple[str, ...]:
    return tuple(token for token in sorted(FORBIDDEN_DOC_TOKENS) if token in text)


def _scan_tokens(path: Path, text: str | None = None) -> list[Violation]:
    payload = _read_text(path) if text is None else text
    return [Violation(path, token, "legacy token") for token in _token_hits(payload)]


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path


def _scan_deleted_roots(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for relative in DELETED_ROOTS:
        path = root / relative
        if path.exists():
            violations.append(Violation(path, relative, "deleted package root exists"))
    return violations


def _scan_metadata_file(path: Path) -> list[Violation]:
    if not path.is_file():
        return []
    text = _read_text(path)
    violations: list[Violation] = []
    for token in (*FORBIDDEN_DISTRIBUTIONS, FORBIDDEN_CLI, FORBIDDEN_ENTRY_GROUP):
        if token in text:
            violations.append(Violation(path, token, "legacy metadata"))
    return violations


def _scan_production(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    for relative in RUNTIME_METADATA_FILES:
        path = root / relative
        if path.is_file() and not _is_allowed(root, path, allowlist):
            violations.extend(_scan_metadata_file(path))
    for relative in RUNTIME_PACKAGE_ROOTS:
        package_root = root / relative
        for path in _iter_files(package_root):
            if _is_allowed(root, path, allowlist):
                continue
            if path.suffix == ".py":
                violations.extend(_scan_python_imports(root, path))
                violations.extend(
                    Violation(path, token, "legacy token")
                    for token in _token_hits(_read_text(path))
                    if token in {FORBIDDEN_CLI, FORBIDDEN_ENTRY_GROUP, FORBIDDEN_HOOKS}
                )
            elif path.suffix in {".yaml", ".yml"}:
                text = _read_text(path)
                if FORBIDDEN_DEFAULT_GRAPH in text:
                    violations.append(Violation(path, FORBIDDEN_DEFAULT_GRAPH, "default graph"))
                violations.extend(
                    Violation(path, token, "legacy token")
                    for token in _token_hits(text)
                    if token in {FORBIDDEN_CLI, FORBIDDEN_ENTRY_GROUP, FORBIDDEN_HOOKS}
                )
    return violations


def _scan_current_docs(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    for relative in CURRENT_DOC_FILES:
        path = root / relative
        if path.is_file() and not _is_allowed(root, path, allowlist):
            violations.extend(_scan_tokens(path))
    return violations


def _scan_examples(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    examples = root / "examples"
    for path in _iter_files(examples):
        if _is_allowed(root, path, allowlist):
            continue
        if path.suffix == ".py":
            violations.extend(_scan_python_imports(root, path))
        if path.suffix in {".py", ".toml", ".md", ".yml", ".yaml", ".txt"}:
            violations.extend(_scan_tokens(path))
    return violations


def _scan_leftover_tests(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    leftovers = [root / "tests/unit", root / "tests/integration", root / "tests/helpers"]
    tests_root = root / "tests"
    if tests_root.is_dir():
        leftovers.extend(sorted(tests_root.glob("helpers_*.py")))
    for base in leftovers:
        for path in _iter_files(base):
            if _is_allowed(root, path, allowlist):
                continue
            if path.suffix == ".py":
                violations.extend(_scan_python_imports(root, path))
    return violations


def _scan_scripts(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    scripts = root / "scripts"
    for path in _iter_files(scripts):
        if path.name in DETECTION_SCRIPT_NAMES or _is_allowed(root, path, allowlist):
            continue
        if path.suffix == ".py":
            violations.extend(_scan_python_imports(root, path))
        text = _read_text(path)
        if FORBIDDEN_RUNTIME_SELECTOR in text:
            violations.append(Violation(path, FORBIDDEN_RUNTIME_SELECTOR, "runtime selector"))
        if f"{FORBIDDEN_CLI} compile" in text or f"{FORBIDDEN_CLI} bindings" in text:
            violations.append(Violation(path, FORBIDDEN_CLI, "legacy command invocation"))
        if f"{FORBIDDEN_CLI} =" in text and "must" not in text:
            violations.append(Violation(path, FORBIDDEN_CLI, "legacy console script"))
    return violations


def _scan_ci(root: Path, allowlist: set[str]) -> list[Violation]:
    path = root / ".github/workflows/ci.yml"
    if not path.is_file() or _is_allowed(root, path, allowlist):
        return []
    return _scan_tokens(path)


def _scan_historical(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    for relative in ("docs", ".superpowers/sdd"):
        for path in _iter_files(root / relative):
            if _is_allowed(root, path, allowlist):
                continue
            if path.suffix not in {".md", ".txt", ".json", ".yaml", ".yml", ".html"}:
                continue
            violations.extend(_scan_tokens(path))
    return violations


def scan_repository(
    root: Path,
    allowlist: tuple[Path, ...] = (),
    scope: Literal["runtime", "repository"] = "repository",
) -> tuple[Violation, ...]:
    allowed = {Path(item).as_posix() for item in allowlist}
    violations = _scan_deleted_roots(root)
    violations.extend(_scan_production(root, allowed))
    if scope == "repository":
        violations.extend(_scan_current_docs(root, allowed))
        violations.extend(_scan_examples(root, allowed))
        violations.extend(_scan_leftover_tests(root, allowed))
        violations.extend(_scan_scripts(root, allowed))
        violations.extend(_scan_ci(root, allowed))
        violations.extend(_scan_historical(root, allowed))
    unique = {(item.path, item.token, item.reason): item for item in violations}
    return tuple(sorted(unique.values(), key=lambda item: (_relative(root, item.path), item.token)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--allowlist", type=Path, default=None)
    parser.add_argument("--scope", choices=("runtime", "repository"), required=True)
    args = parser.parse_args(argv)
    repo = (args.repo or Path(__file__).resolve().parents[1]).resolve()
    allowlist_path = args.allowlist or (repo / "scripts/no_legacy_allowlist.txt")
    allowlist = load_allowlist(allowlist_path, root=repo) if allowlist_path.is_file() else ()
    violations = scan_repository(repo, allowlist, scope=args.scope)
    if not violations:
        print(f"no-legacy {args.scope}: OK")
        return 0
    for item in violations:
        location = _relative(repo, item.path)
        print(f"{location}: {item.token} ({item.reason})", file=sys.stderr)
    print(f"no-legacy {args.scope}: {len(violations)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
