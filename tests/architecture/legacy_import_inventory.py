from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PLAN_RELATIVE = Path("docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md")

SCAN_ROOTS = ("packages", "tests", "examples", "benchmark", "scripts")
SCAN_SUFFIXES = frozenset({".py", ".sh"})
GENERATED_BENCHMARK_ROOTS = frozenset(
    {
        "benchmark/assurance-product/results",
        "benchmark/vue-fastapi-admin/eval/out",
        "benchmark/vue-fastapi-admin/.aa/cache",
    }
)
NON_SOURCE_DIRECTORIES = frozenset({"__pycache__", ".venv", "venv", "node_modules", ".runtime", ".staging"})

FORBIDDEN_SYMBOLS = frozenset(
    {
        "WorkflowModuleDef",
        "GraphDef",
        "NodeDef",
        "CompiledWorkflow",
        "compile_workflow",
        "assemble_product_workflow",
    }
)
FORBIDDEN_MODULES = frozenset(
    {
        "graph_engine.graph.expressions",
        "graph_engine.graph.input_projection",
        "graph_engine.graph.output_projection",
    }
)
RUNTIME_AUTHORITY_PREFIX = "graph_engine.runtime"

_TASK_HEADINGS = (
    "### Task 7: Prepare one atomic compiler/Runtime authority deletion",
    "### Task 8: Atomically switch compile to v3-only and delete Workflow YAML, module packaging, and phase aliases",
    "### Task 9: Atomically delete the Workflow compiler and custom Runtime",
    "### Task 10: Remove migration switches and update docs/packaging/smoke tests",
)

_DISPOSITION_RE = re.compile(
    r"^- (?P<disposition>Retain unchanged until Task 9|Retain until Task 9|"
    r"Retain as an explicitly allowlisted temporary leftover execution adapter until Task 9|"
    r"Retain as an explicitly allowlisted temporary legacy execution adapter until Task 9|"
    r"Retain/modify imports only|Retain|"
    r"Modify only to add/use the coexistence Python branch; retain every leftover field/branch until Task 9|"
    r"Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9|"
    r"Modify as temporary re-export wrappers(?: only for modules actually moved above)?|"
    r"Modify only to cover the remaining consumer migration; preserve the focused Raw Agent runtime tests|"
    r"Modify only if Task 7 import migration requires it|"
    r"Modify/regenerate|Modify|Delete/replace|Delete Task 7 compatibility wrapper|"
    r"Delete after all moves/import rewrites|Delete|Create in Task 7(?: only if retained consumers require them)?|"
    r"Create): `(?P<path>[^`]+)`"
)


@dataclass(frozen=True, slots=True)
class InventoryHit:
    path: str
    kind: str
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class InventoryAllowlist:
    retained_implementations: frozenset[str]
    task8_consumers: frozenset[str]
    task9_characterization: frozenset[str]

    @property
    def paths(self) -> frozenset[str]:
        return self.retained_implementations | self.task9_characterization


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_repo_path(value: str) -> str | None:
    path = value.strip()
    if not path or path.endswith("/"):
        return None
    if path in {"uv.lock", ".github/workflows/ci.yml"}:
        return path
    if path.endswith((".py", ".sh", ".yaml", ".yml", ".json", ".toml")):
        return path
    return None


def _task_section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def _files_block(section: str) -> str:
    marker = "**Files:**"
    start = section.index(marker)
    rest = section[start + len(marker) :]
    end = rest.find("\n**")
    return rest if end < 0 else rest[:end]


def _iter_disposition_paths(block: str) -> Iterator[tuple[str, str]]:
    for raw_line in block.splitlines():
        line = raw_line.strip()
        match = _DISPOSITION_RE.match(line)
        if match is None:
            continue
        path = _normalize_repo_path(match.group("path"))
        if path is None:
            continue
        yield match.group("disposition"), path


_LEFTOVER_ATTEMPT_HOMES = frozenset(
    {
        "packages/framework/graph-engine/graph_engine/attempts/activity.py",
        "packages/framework/graph-engine/graph_engine/attempts/host_receipts.py",
        "packages/framework/graph-engine/graph_engine/attempts/production_host.py",
    }
)


def _task7_category_a(disposition: str, path: str) -> bool:
    if disposition.startswith("Create"):
        return path.startswith("tests/architecture/")
    return (
        disposition.startswith("Retain")
        or disposition.startswith("Modify only to add/use the coexistence")
        or "re-export wrappers" in disposition
    )


def _task7_leftover_attempt_home(disposition: str, path: str) -> bool:
    return disposition.startswith("Create in Task 7") and path in _LEFTOVER_ATTEMPT_HOMES


def _task8_counted(disposition: str) -> bool:
    return disposition in {"Modify", "Modify/regenerate", "Delete", "Delete/replace", "Create"}


def _task9_counted(disposition: str) -> bool:
    return disposition.startswith("Modify") or disposition.startswith("Delete") or disposition == "Create"


def load_explicit_allowlist(plan_text: str | None = None) -> InventoryAllowlist:
    text = (repo_root() / PLAN_RELATIVE).read_text(encoding="utf-8") if plan_text is None else plan_text
    task7 = _task_section(text, _TASK_HEADINGS[0], _TASK_HEADINGS[1])
    task8 = _task_section(text, _TASK_HEADINGS[1], _TASK_HEADINGS[2])
    task9 = _task_section(text, _TASK_HEADINGS[2], _TASK_HEADINGS[3])

    retained: set[str] = set()
    for disposition, path in _iter_disposition_paths(_files_block(task7)):
        if _task7_category_a(disposition, path) or _task7_leftover_attempt_home(disposition, path):
            retained.add(path)

    task8_paths = {
        path
        for disposition, path in _iter_disposition_paths(_files_block(task8))
        if _task8_counted(disposition)
    }
    task9_paths: set[str] = set()
    for disposition, path in _iter_disposition_paths(_files_block(task9)):
        if disposition.startswith("Retain"):
            retained.add(path)
        elif _task9_counted(disposition):
            task9_paths.add(path)

    return InventoryAllowlist(
        retained_implementations=frozenset(retained),
        task8_consumers=frozenset(task8_paths),
        task9_characterization=frozenset(task9_paths),
    )


def _is_runtime_authority(module: str) -> bool:
    return module == RUNTIME_AUTHORITY_PREFIX or module.startswith(f"{RUNTIME_AUTHORITY_PREFIX}.")


def _iter_python_files(root: Path) -> Iterator[Path]:
    for name in SCAN_ROOTS:
        base = root / name
        if not base.exists():
            continue
        for directory, children, files in os.walk(base):
            current = Path(directory)
            children[:] = sorted(
                child
                for child in children
                if child not in NON_SOURCE_DIRECTORIES
                and (current / child).relative_to(root).as_posix() not in GENERATED_BENCHMARK_ROOTS
            )
            for filename in sorted(files):
                path = current / filename
                if path.is_file() and path.suffix in SCAN_SUFFIXES:
                    yield path


def _embedded_python_from_shell(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    heredoc = re.compile(r"<<['\"]PY['\"]\s*\n(.*?)(?:\nPY\s*$|\nPY\n)", re.DOTALL | re.MULTILINE)
    blocks.extend(heredoc.findall(text))
    return tuple(blocks)


def _hits_from_module(path: str, tree: ast.AST) -> tuple[InventoryHit, ...]:
    hits: list[InventoryHit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_runtime_authority(alias.name) or alias.name in FORBIDDEN_MODULES:
                    hits.append(InventoryHit(path=path, kind="import", name=alias.name, line=node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if _is_runtime_authority(module) or module in FORBIDDEN_MODULES:
                imported = ", ".join(alias.name for alias in node.names)
                hits.append(
                    InventoryHit(
                        path=path,
                        kind="from-module",
                        name=f"{module}:{imported}",
                        line=node.lineno,
                    )
                )
            for alias in node.names:
                if alias.name in FORBIDDEN_SYMBOLS:
                    hits.append(
                        InventoryHit(
                            path=path,
                            kind="symbol",
                            name=f"{module}.{alias.name}",
                            line=node.lineno,
                        )
                    )
    return tuple(hits)


def scan_legacy_imports(root: Path | None = None) -> tuple[InventoryHit, ...]:
    base = repo_root() if root is None else root
    hits: list[InventoryHit] = []
    for path in _iter_python_files(base):
        relative = path.relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sh":
            for index, block in enumerate(_embedded_python_from_shell(text)):
                try:
                    tree = ast.parse(block)
                except SyntaxError as error:
                    hits.append(
                        InventoryHit(
                            path=relative,
                            kind="parse-error",
                            name=f"embedded[{index}]:{error.msg}",
                            line=error.lineno or 0,
                        )
                    )
                    continue
                hits.extend(_hits_from_module(relative, tree))
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            hits.append(
                InventoryHit(
                    path=relative,
                    kind="parse-error",
                    name=error.msg,
                    line=error.lineno or 0,
                )
            )
            continue
        hits.extend(_hits_from_module(relative, tree))
    return tuple(hits)


def unallowlisted_hits(
    hits: tuple[InventoryHit, ...],
    allowlist: InventoryAllowlist,
) -> tuple[InventoryHit, ...]:
    allowed = allowlist.paths
    return tuple(hit for hit in hits if hit.kind == "parse-error" or hit.path not in allowed)


def allowlist_is_explicit(allowlist: InventoryAllowlist) -> bool:
    return not any("*" in path or path.endswith("/") for path in allowlist.paths)


__all__ = [
    "FORBIDDEN_MODULES",
    "FORBIDDEN_SYMBOLS",
    "InventoryAllowlist",
    "InventoryHit",
    "allowlist_is_explicit",
    "load_explicit_allowlist",
    "repo_root",
    "scan_legacy_imports",
    "unallowlisted_hits",
]
