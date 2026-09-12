"""Bounded, read-only source observations for case and implementation planning.

This index never imports SUT code or infers behavior from a symbol's name. Missing
observations mean unknown, not absent. Consumers still verify semantic claims.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

_MAX_BYTES = 256 * 1024
_MAX_FILES = 64
_SOURCE_PATH = re.compile(r"(?<![\w./-])(?:[A-Za-z_][\w-]*/)+[\w.-]+\.(?:py|tsx?|jsx?|vue)(?![\w/])")
_FAMILY_ROOTS = {"api": "api", "e2e": "e2e", "fuzz": "fuzz", "performance": "perf"}
_QA_TESTS_PREFIX = "qa/tests/"
_TESTS_PREFIX = "tests/"
_FACTS_LIMITS = (
    "Static observations only; unknown is not absent. Environment names are "
    "references, not proof of availability or necessity. Independently verify "
    "source behavior and owner-defined oracles. Fixtures and support modules "
    "live under qa/tests/. The execution view remaps qa/tests/ to tests/ for "
    "pytest collection."
)


def _durable_test_path(relative: str) -> str | None:
    path = PurePosixPath(relative)
    if path.is_absolute() or path.as_posix() != relative or ".." in path.parts:
        return None
    if relative.startswith(_QA_TESTS_PREFIX):
        return relative
    if relative.startswith(_TESTS_PREFIX):
        return _QA_TESTS_PREFIX + relative[len(_TESTS_PREFIX) :]
    return None


def _read(root: Path, relative: str) -> tuple[bytes | None, str | None]:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or path.as_posix() != relative
        or any(part in {"..", ".git", ".env"} for part in path.parts)
    ):
        return None, "unsafe_path"
    current = root
    try:
        for part in path.parts:
            current /= part
            if current.is_symlink():
                return None, "symlink"
        if not current.is_file():
            return None, "not_observed"
        with current.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return None, "size_limit"
        return data, None
    except OSError:
        return None, "unreadable"


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    # Defaults and annotations may contain credentials; expose shape only.
    args = copy.deepcopy(node.args)
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        arg.annotation = None
    if args.vararg:
        args.vararg.annotation = None
    if args.kwarg:
        args.kwarg.annotation = None
    args.defaults = [ast.Constant(value=Ellipsis) for _ in args.defaults]
    args.kw_defaults = [None if value is None else ast.Constant(value=Ellipsis) for value in args.kw_defaults]
    return f"{node.name}({ast.unparse(args)})"


def _python_facts(data: bytes, relative: str) -> dict[str, Any]:
    try:
        tree = ast.parse(data, filename=relative)
    except (SyntaxError, ValueError):
        return {"parse_status": "unknown", "symbols": [], "environment_names": []}
    fixture_decorators = {"pytest.fixture", "pytest_asyncio.fixture"}
    environment_calls = {"os.getenv", "os.environ.get"}
    environment_maps = {"os.environ"}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name
                if alias.name in {"pytest", "pytest_asyncio"}:
                    fixture_decorators.add(f"{name}.fixture")
                if alias.name == "os":
                    environment_calls.update((f"{name}.getenv", f"{name}.environ.get"))
                    environment_maps.add(f"{name}.environ")
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                name = alias.asname or alias.name
                if statement.module in {"pytest", "pytest_asyncio"} and alias.name == "fixture":
                    fixture_decorators.add(name)
                if statement.module == "os" and alias.name == "getenv":
                    environment_calls.add(name)
                if statement.module == "os" and alias.name == "environ":
                    environment_calls.add(f"{name}.get")
                    environment_maps.add(name)
    symbols: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        fixture_name = None
        for decorator in node.decorator_list:
            function = decorator.func if isinstance(decorator, ast.Call) else decorator
            if ast.unparse(function) not in fixture_decorators:
                continue
            fixture_name = node.name
            if isinstance(decorator, ast.Call):
                for keyword in decorator.keywords:
                    if keyword.arg == "name":
                        value = keyword.value
                        fixture_name = (
                            value.value
                            if isinstance(value, ast.Constant) and isinstance(value.value, str)
                            else None
                        )
        symbols.append(
            {
                "name": node.name,
                "line": node.lineno,
                "signature": _signature(node),
                "fixture_name": fixture_name,
            }
        )
    environment: set[str] = set()
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Call) and ast.unparse(node.func) in environment_calls and node.args:
            key = node.args[0]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in environment_maps:
            key = node.slice
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", key.value)
        ):
            environment.add(key.value)
    return {"parse_status": "observed", "symbols": symbols, "environment_names": sorted(environment)}


def build_planning_facts(
    workspace: Path,
    *,
    change_id: str,
    capability_leafs: tuple[str, ...],
    families: tuple[str, ...],
    target_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Observe explicit source hints, declared test symbols and ancestor fixtures.

    No repository-wide search, ambient environment, SUT import, or model summary
    participates. The digest binds observations, not their semantic correctness.
    """
    root = workspace.resolve()
    candidates = {"qa/tests/conftest.py", "qa/tests/config.py"}
    candidates.update(f"qa/tests/{_FAMILY_ROOTS[f]}/conftest.py" for f in families if f in _FAMILY_ROOTS)
    inputs: list[dict[str, str]] = []
    for relative in ("qa/requirement.md", "qa/proposal.md", "qa/results/explore/exploration.json"):
        data, _ = _read(root, relative)
        if data is None:
            continue
        inputs.append({"path": relative, "digest": hashlib.sha256(data).hexdigest()})
        candidates.update(_SOURCE_PATH.findall(data.decode("utf-8", errors="replace")))
    knowledge_path = ".aa/data-knowledge.yaml"
    knowledge_bytes, _ = _read(root, knowledge_path)
    knowledge: Any = {}
    if knowledge_bytes is not None:
        inputs.append({"path": knowledge_path, "digest": hashlib.sha256(knowledge_bytes).hexdigest()})
        try:
            knowledge = yaml.safe_load(knowledge_bytes)
        except (yaml.YAMLError, UnicodeError):
            knowledge = {}
    declared: list[dict[str, Any]] = []
    excluded_roots = {root_name for family, root_name in _FAMILY_ROOTS.items() if family not in families}
    for capability in sorted(set(capability_leafs)):
        leaf = knowledge
        for part in capability.split("."):
            leaf = leaf.get(part) if isinstance(leaf, dict) else None
        symbol = leaf.get("symbol") if isinstance(leaf, dict) else None
        if not isinstance(symbol, str) or not re.fullmatch(r"tests(?:\.[A-Za-z_]\w*){2,}", symbol):
            continue
        module, _, name = symbol.rpartition(".")
        if module.split(".")[1] in excluded_roots:
            continue
        relative = _durable_test_path(module.replace(".", "/") + ".py")
        if relative is None:
            continue
        candidates.add(relative)
        declared.append({"capability": capability, "symbol": symbol, "path": relative, "name": name})
    for relative in target_files:
        durable = _durable_test_path(relative)
        if durable is None:
            continue
        candidates.add(durable)
        for parent in PurePosixPath(durable).parents:
            if parent.as_posix() != ".":
                candidates.add(f"{parent}/conftest.py")
    files: list[dict[str, Any]] = []
    for relative in sorted(candidates)[:_MAX_FILES]:
        data, reason = _read(root, relative)
        fact: dict[str, Any] = {"path": relative, "status": "unknown" if data is None else "observed"}
        if data is None:
            fact["reason"] = reason
        else:
            fact["digest"] = hashlib.sha256(data).hexdigest()
            if relative.endswith(".py"):
                fact.update(_python_facts(data, relative))
        files.append(fact)
    by_path = {file["path"]: file for file in files}
    for item in declared:
        definitions = by_path.get(item["path"], {}).get("symbols", [])
        item["status"] = "observed" if any(s["name"] == item["name"] for s in definitions) else "unknown"
    payload = {
        "capability_leafs": sorted(set(capability_leafs)),
        "inputs": inputs,
        "files": files,
        "declared_symbols": declared,
        "uninspected_paths": sorted(candidates)[_MAX_FILES:],
        "limits": _FACTS_LIMITS,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**payload, "digest": digest}
