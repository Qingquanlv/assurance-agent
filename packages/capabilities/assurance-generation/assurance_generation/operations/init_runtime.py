"""Create-if-missing QA test harness and L1 testdata/adapter skeletons."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.contracts.init_runtime import InitTestRuntimeInputV1, InitTestRuntimeResultV1
from assurance_generation.resource_loader import resource_bytes

DATA_KNOWLEDGE_PATH = ".aa/data-knowledge.yaml"
DATA_KNOWLEDGE_RESOURCE_ID = "assurance.product.configuration.data-knowledge"
HARNESS_PATHS: tuple[str, ...] = (
    "qa/tests/__init__.py",
    "qa/tests/conftest.py",
    "qa/tests/config.py",
    "qa/tests/schema_validation.py",
    "qa/tests/api/__init__.py",
    "qa/tests/api/conftest.py",
    "qa/tests/e2e/__init__.py",
    "qa/tests/e2e/conftest.py",
    "qa/tests/fuzz/__init__.py",
    "qa/tests/fuzz/conftest.py",
    "qa/tests/perf/__init__.py",
    "qa/tests/perf/conftest.py",
)
_HARNESS_PATH_SET = frozenset(HARNESS_PATHS)
TESTDATA_PACKAGE_PATHS: tuple[str, ...] = (
    "qa/tests/testdata/__init__.py",
    "qa/tests/testdata/domain/__init__.py",
)
_TESTDATA_ROOT = PurePosixPath("qa/tests/testdata")
_RECEIPT_PATH = "qa/results/init/test-runtime.json"
_PACKAGE_DOC = b'"""Test runtime package."""\n'
_L1_PREFIXES = (
    "tests.testdata.",
    "tests.api.adapters.",
    "tests.e2e.adapters.",
    "tests.fuzz.adapters.",
    "tests.perf.adapters.",
)
_FORBIDDEN_NAME = re.compile(r"^(test_|locustfile)")
WriteAction = Literal["created", "skipped"]


@dataclass(frozen=True, slots=True)
class L1Symbol:
    symbol: str
    kind: str = ""
    notes: str | None = None


def collect_l1_symbols(document: object) -> tuple[L1Symbol, ...]:
    found: list[L1Symbol] = []
    _collect_l1_symbols(document, found)
    found.sort(key=lambda item: item.symbol)
    return tuple(found)


def symbol_to_repo_path(symbol: str) -> tuple[str, str]:
    if (
        not symbol
        or ".." in symbol
        or symbol.startswith(("/", "\\", "~"))
        or "\\" in symbol
        or "\x00" in symbol
    ):
        raise ValueError("unsafe symbol")
    parts = symbol.split(".")
    if len(parts) < 3 or parts[0] != "tests" or any(not part for part in parts):
        raise ValueError("unsafe symbol")
    *module_parts, name = parts[1:]
    for part in (*module_parts, name):
        if _FORBIDDEN_NAME.match(part):
            raise ValueError(f"unsafe symbol starts with test_ or locustfile: {part}")
        if part.startswith("/") or part in {".", ".."}:
            raise ValueError("unsafe symbol")
    relative = PurePosixPath("qa", "tests", *module_parts)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("unsafe symbol")
    try:
        relative.relative_to(PurePosixPath("qa", "tests"))
    except ValueError as error:
        raise ValueError("unsafe symbol escapes qa/tests/") from error
    return f"{relative.as_posix()}.py", name


def init_test_runtime(
    request: InitTestRuntimeInputV1, project_root: Path, write_root: Path
) -> InitTestRuntimeResultV1:
    document = _authenticate_knowledge(request, project_root)
    symbols = collect_l1_symbols(document)
    grouped: dict[str, list[L1Symbol]] = {}
    package_inits: set[str] = set(TESTDATA_PACKAGE_PATHS)
    for item in symbols:
        module_path, _name = symbol_to_repo_path(item.symbol)
        if _is_testdata_entity_module(module_path):
            continue
        grouped.setdefault(module_path, []).append(item)
        package_inits.update(_package_inits_for(module_path))

    harness_created: list[str] = []
    harness_skipped: list[str] = []
    for relative in HARNESS_PATHS:
        action = _write_or_skip(
            project_root,
            write_root,
            relative,
            resource_bytes(f"test-runtime/{relative.removeprefix('qa/')}"),
        )
        (harness_created if action == "created" else harness_skipped).append(relative)

    symbol_created: list[str] = []
    symbol_skipped: list[str] = []
    for init_path in sorted(package_inits):
        action = _write_or_skip(project_root, write_root, init_path, _PACKAGE_DOC)
        (symbol_created if action == "created" else symbol_skipped).append(init_path)
    for module_path, items in grouped.items():
        action = _write_or_skip(project_root, write_root, module_path, _render_module(items).encode("utf-8"))
        (symbol_created if action == "created" else symbol_skipped).append(module_path)

    result = InitTestRuntimeResultV1(
        change_id=request.change_id,
        harness_created=tuple(harness_created),
        harness_skipped=tuple(harness_skipped),
        symbol_files_created=tuple(symbol_created),
        symbol_files_skipped=tuple(symbol_skipped),
        symbols_declared=tuple(item.symbol for item in symbols),
    )
    _write_bytes(write_root, _RECEIPT_PATH, canonical_json_bytes(_json_result(result)) + b"\n")
    return result


class InitTestRuntimeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = InitTestRuntimeInputV1.model_validate(request.input)
            result = init_test_runtime(payload, context.project_root, context.write_root)
            return TaskOutcome.succeeded(result.model_dump(mode="json"))
        except (ValueError, ValidationError, OSError, yaml.YAMLError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)


def _collect_l1_symbols(node: object, found: list[L1Symbol]) -> None:
    if isinstance(node, Mapping):
        symbol = node.get("symbol")
        if isinstance(symbol, str) and symbol.startswith(_L1_PREFIXES):
            kind = node.get("kind")
            notes = node.get("notes")
            found.append(
                L1Symbol(
                    symbol=symbol,
                    kind=kind if isinstance(kind, str) else "",
                    notes=notes if isinstance(notes, str) else None,
                )
            )
        for value in node.values():
            _collect_l1_symbols(value, found)
        return
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for item in node:
            _collect_l1_symbols(item, found)


def _authenticate_knowledge(request: InitTestRuntimeInputV1, project_root: Path) -> Mapping[str, object]:
    if request.data_knowledge.resource_id != DATA_KNOWLEDGE_RESOURCE_ID:
        raise ValueError("data knowledge resource_id is not authenticated")
    path = _workspace_path(project_root, DATA_KNOWLEDGE_PATH)
    if path.is_symlink() or not path.is_file():
        raise ValueError("data knowledge is missing or not a regular file")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != request.data_knowledge.sha256:
        raise ValueError("data knowledge digest mismatch")
    document = yaml.safe_load(data)
    if not isinstance(document, Mapping):
        raise ValueError("data knowledge must be a mapping")
    return document


def _is_testdata_entity_module(module_path: str) -> bool:
    path = PurePosixPath(module_path)
    try:
        path.relative_to(_TESTDATA_ROOT)
    except ValueError:
        return False
    return path.name != "__init__.py"


def _package_inits_for(module_path: str) -> tuple[str, ...]:
    parent = PurePosixPath(module_path).parent
    inits: list[str] = []
    while parent.as_posix() not in {"qa/tests", "qa", "."} and parent.parts:
        init = f"{parent.as_posix()}/__init__.py"
        if init not in _HARNESS_PATH_SET:
            inits.append(init)
        parent = parent.parent
    return tuple(inits)


def _render_module(items: Sequence[L1Symbol]) -> str:
    blocks = ["from __future__ import annotations", ""]
    for item in items:
        _path, name = symbol_to_repo_path(item.symbol)
        prefix = "async def" if item.kind == "async_factory" else "def"
        doc = item.notes or item.symbol
        blocks.append(
            f"{prefix} {name}(*args, **kwargs):\n"
            f'    """{doc}"""\n'
            f'    raise NotImplementedError("assurance.init: {item.symbol}")\n'
        )
    return "\n".join(blocks) + "\n"


def _write_or_skip(project_root: Path, write_root: Path, relative: str, data: bytes) -> WriteAction:
    durable = _workspace_path(project_root, relative)
    if durable.is_symlink() or (durable.exists() and not durable.is_file()):
        raise ValueError(f"existing target is not a regular file: {relative}")
    if durable.is_file():
        return "skipped"
    _write_bytes(write_root, relative, data)
    return "created"


def _write_bytes(root: Path, relative: str, data: bytes) -> None:
    path = _workspace_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _workspace_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe path: {relative}")
    return root.joinpath(*path.parts)


def _json_result(result: InitTestRuntimeResultV1) -> JSONValue:
    return result.model_dump(mode="json")
