"""Scan the current tests tree for coverage facts (no workflow imports)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.trace import TraceTestRef
from assurance_agent.evidence.case_id import extract_case_id

_TESTS_ROOT = "tests"
_PERF_DIR = "perf"
_LOCUSTFILE_PREFIX = "locustfile"

_TEST_DEF = re.compile(r"^\s*(?:async\s+def|def)\s+(test_\w+)")
_TASK_NAME = re.compile(r"""(?<![A-Za-z0-9_])name\s*=\s*(?P<quote>["'])(?P<capability>[^"']+)(?P=quote)""")


@dataclass(frozen=True)
class TreeScanResult:
    case_ids: frozenset[str]
    perf_capabilities: frozenset[str]
    functions: tuple[TraceTestRef, ...]
    covering_tests: Mapping[str, tuple[TraceTestRef, ...]]
    file_sha256: Mapping[str, str]
    tree_digest: str
    function_refs: frozenset[tuple[str, str]] = frozenset()

    @property
    def case_refs(self) -> Mapping[str, tuple[TraceTestRef, ...]]:
        """V2 fold alias for the established ``covering_tests`` mapping."""
        return self.covering_tests


def _is_ignored(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _strip_comment(line: str) -> str:
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
        index += 1
    return line


def _test_function_names(lines: Iterable[str]) -> list[str]:
    matches = (_TEST_DEF.match(line) for line in lines)
    return [match.group(1) for match in matches if match is not None]


def _capability_literals(lines: Iterable[str]) -> list[str]:
    return [
        match.group("capability") for line in lines for match in _TASK_NAME.finditer(_strip_comment(line))
    ]


def _is_locustfile(path: Path, tests_root: Path) -> bool:
    return path.parent == tests_root / _PERF_DIR and path.name.startswith(_LOCUSTFILE_PREFIX)


def _ref_key(ref: TraceTestRef) -> tuple[str, str]:
    return (ref.file, ref.test_name)


def scan_test_tree(project_root: Path) -> TreeScanResult:
    tests_root = project_root / _TESTS_ROOT
    file_sha256: dict[str, str] = {}
    functions: list[TraceTestRef] = []
    covering: dict[str, list[TraceTestRef]] = {}
    capabilities: set[str] = set()

    for path in sorted(tests_root.rglob("*")) if tests_root.is_dir() else []:
        if not path.is_file() or _is_ignored(path):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(project_root).as_posix()
        file_sha256[rel] = hashlib.sha256(content).hexdigest()
        if path.suffix != ".py":
            continue
        lines = content.decode("utf-8", errors="replace").splitlines()
        for name in _test_function_names(lines):
            ref = TraceTestRef(file=rel, test_name=name)
            functions.append(ref)
            case_id = extract_case_id(name)
            if case_id:
                covering.setdefault(case_id, []).append(ref)
        if _is_locustfile(path, tests_root):
            capabilities.update(_capability_literals(lines))

    covering_tests = {
        case_id: tuple(sorted(refs, key=_ref_key)) for case_id, refs in sorted(covering.items())
    }
    ordered_functions = tuple(sorted(functions, key=_ref_key))
    return TreeScanResult(
        case_ids=frozenset(covering_tests),
        perf_capabilities=frozenset(capabilities),
        functions=ordered_functions,
        covering_tests=covering_tests,
        file_sha256=dict(sorted(file_sha256.items())),
        tree_digest=hashlib.sha256(canonical_json_bytes(dict(sorted(file_sha256.items())))).hexdigest(),
        function_refs=frozenset(_ref_key(ref) for ref in ordered_functions),
    )
