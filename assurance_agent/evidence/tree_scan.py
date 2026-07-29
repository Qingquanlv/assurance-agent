"""Scan the current tests tree for coverage facts (no workflow imports)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from assurance_agent.artifacts.models.trace import TraceTestRef
from assurance_agent.evidence.case_id import extract_case_id

_TEST_DEF = re.compile(r"^\s*(?:async\s+def|def)\s+(test_\w+)", re.MULTILINE)
_PERF_NAME = re.compile(r"""\bname\s*=\s*['"]([^'"]+)['"]""")
_COMMENT_LINE = re.compile(r"^\s*#")


@dataclass(frozen=True)
class TreeScanResult:
    case_ids: frozenset[str]
    perf_capabilities: frozenset[str]
    functions: tuple[str, ...]
    file_sha256: Mapping[str, str]
    tree_digest: str
    case_refs: Mapping[str, tuple[TraceTestRef, ...]]
    function_refs: frozenset[tuple[str, str]]


def scan_test_tree(project_root: Path) -> TreeScanResult:
    tests_root = project_root / "tests"
    file_sha256: dict[str, str] = {}
    functions: list[str] = []
    case_to_refs: dict[str, list[TraceTestRef]] = defaultdict(list)
    function_refs: set[tuple[str, str]] = set()
    perf_capabilities: set[str] = set()

    if tests_root.is_dir():
        for path in sorted(tests_root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(project_root).as_posix()
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            file_sha256[rel] = hashlib.sha256(raw).hexdigest()
            if path.suffix != ".py":
                continue
            text = raw.decode("utf-8", errors="replace")
            _scan_python_file(rel, text, functions, case_to_refs, function_refs, perf_capabilities)

    tree_digest = hashlib.sha256(
        json.dumps(dict(sorted(file_sha256.items())), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    case_refs = {case_id: tuple(refs) for case_id, refs in sorted(case_to_refs.items())}
    return TreeScanResult(
        case_ids=frozenset(case_refs),
        perf_capabilities=frozenset(perf_capabilities),
        functions=tuple(functions),
        file_sha256=file_sha256,
        tree_digest=tree_digest,
        case_refs=case_refs,
        function_refs=frozenset(function_refs),
    )


def _scan_python_file(
    rel: str,
    text: str,
    functions: list[str],
    case_to_refs: dict[str, list[TraceTestRef]],
    function_refs: set[tuple[str, str]],
    perf_capabilities: set[str],
) -> None:
    for match in _TEST_DEF.finditer(text):
        fn = match.group(1)
        functions.append(fn)
        function_refs.add((rel, fn))
        case_id = extract_case_id(fn)
        if case_id:
            case_to_refs[case_id].append(TraceTestRef(file=rel, function=fn))

    # Performance capability: house rule name="<capability>" on non-comment lines.
    # Locust tasks live under tests/perf/; still accept name= anywhere under tests/
    # so unit fixtures stay simple, but never from comment-only lines.
    for line in text.splitlines():
        if _COMMENT_LINE.match(line):
            continue
        code = line.split("#", 1)[0]
        for match in _PERF_NAME.finditer(code):
            perf_capabilities.add(match.group(1))
