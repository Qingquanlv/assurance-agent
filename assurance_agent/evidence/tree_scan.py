"""Scan the *current* ``tests/`` tree for the facts coverage is derived from.

``covered`` means "a test mapping this case exists in the tree right now"
(spec v3 §7). A pytest report only proves a test *ran*, never that it still
exists, so the fold reads the tree itself — deterministically, without importing
pytest and without executing anything.

Two rules keep the mapping honest:

- **Line-based ``def`` extraction, never a full-text search.** Test modules
  routinely name whole case ranges in a header docstring
  (``Cases: TC_DEPT_E2E_001 - TC_DEPT_E2E_005``) and keep commented-out tests
  around; a full-text scan would read both as coverage. Only the identifier on a
  ``def`` / ``async def`` line is fed to ``extract_case_id``, so the
  ``test_<case_id>__<desc>`` naming contract is the single source of the mapping.
- **Performance is a literal capability hit.** House rule: a Locust task's
  ``name="<capability>"`` label equals the case's
  ``automation.performance.scenario.capability``. Only ``tests/perf/locustfile*.py``
  is searched (the same discovery the runner uses), and only outside comments.

Hashing is deliberately split in two, which is what makes the fold's drift check
unambiguous:

- ``file_sha256`` is the plain SHA-256 of each file's bytes under ``tests/``,
  over the same file set the runner's ``hash_test_tree`` records into
  ``execution-manifest.yaml`` (``__pycache__`` and ``.pyc`` excluded). Because
  both sides are plain per-file digests, comparing them cannot disagree about an
  aggregation algorithm.
- ``tree_digest`` is **evidence-owned**: the SHA-256 of the canonical JSON of the
  sorted ``{path: sha256}`` mapping. It is what the projection records as the
  tests-tree source digest and is never compared against the manifest's
  ``tests_tree_sha256`` (a different aggregation of the same per-file facts).
"""

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
# The left lookbehind makes `name` a whole keyword argument: `username=`,
# `user_name=` and `file_name=` are ordinary parameters, not capability labels.
_TASK_NAME = re.compile(r"""(?<![A-Za-z0-9_])name\s*=\s*(?P<quote>["'])(?P<capability>[^"']+)(?P=quote)""")


@dataclass(frozen=True)
class TreeScanResult:
    """The current tree, as the fold sees it.

    ``functions`` deviates from the brief's ``tuple[str, ...]``: a bare function
    name cannot identify a test, and the projection's ``covering_tests`` is a
    ``TraceTestRef`` (``{file, test_name}``). Every discovered test function is
    listed here, whether or not it maps a case; ``covering_tests`` holds only
    the ones that do, keyed by case id, and ``case_ids`` is exactly its key set.
    Both are sorted by ``(file, test_name)``, so nothing downstream inherits a
    filesystem traversal order.
    """

    case_ids: frozenset[str]
    perf_capabilities: frozenset[str]
    functions: tuple[TraceTestRef, ...]
    covering_tests: Mapping[str, tuple[TraceTestRef, ...]]
    file_sha256: Mapping[str, str]
    tree_digest: str


def _is_ignored(path: Path) -> bool:
    """Mirror ``hash_test_tree``'s exclusions so the two file sets are the same."""
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, leaving ``#`` inside string literals alone."""
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


def _tree_digest(file_sha256: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(sorted(file_sha256.items())))).hexdigest()


def scan_test_tree(project_root: Path) -> TreeScanResult:
    """Scan ``<project_root>/tests`` for coverage facts and per-file digests.

    An absent ``tests/`` directory is an empty tree, not an error: the fold then
    reports every automation-required case ``uncovered`` and — if the batch
    claimed test files — a per-file drift, which is exactly the truth.
    """
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
            # Unreadable files are skipped rather than guessed at, matching
            # ``hash_test_tree``: an absent entry then shows up as drift.
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

    # Sort on the fields themselves rather than trusting traversal order: `Path`
    # ordering is not the posix-string ordering these refs are compared by.
    covering_tests = {
        case_id: tuple(sorted(refs, key=_ref_key)) for case_id, refs in sorted(covering.items())
    }
    return TreeScanResult(
        case_ids=frozenset(covering_tests),
        perf_capabilities=frozenset(capabilities),
        functions=tuple(sorted(functions, key=_ref_key)),
        covering_tests=covering_tests,
        file_sha256=dict(sorted(file_sha256.items())),
        tree_digest=_tree_digest(file_sha256),
    )


__all__ = ["TreeScanResult", "scan_test_tree"]
