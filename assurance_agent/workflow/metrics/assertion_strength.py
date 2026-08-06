"""``operation:compute-assertion-strength`` — nightly B2 assertion strength (§5-B2).

Scans ``tests/api`` and ``tests/e2e`` test functions and classifies each via M1
``verification.assertion_class`` (no second AST taxonomy). Writes batch evidence
at ``execution/runs/nightly/assertion-strength.json`` for ``aggregate-nightly``;
never writes ``inspect/metrics.json``.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import MetricScope, MetricShortboard, PR_METRICS_REL
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AssertionStrengthEvidence,
    AssertionStrengthSurfaceLayer,
    AssertionStrengthSurfaceSlice,
    WeakAssertionLocator,
)
from assurance_agent.config import load_config
from assurance_agent.verification.assertion_class import (
    classify_api_assertions,
    classify_e2e_assertions,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace

ASSERTION_STRENGTH_BATCH_ID = "nightly"
ASSERTION_STRENGTH_EVIDENCE_REL = f"execution/runs/{ASSERTION_STRENGTH_BATCH_ID}/assertion-strength.json"

_DEFAULT_API_DIR = "tests/api"
_DEFAULT_E2E_DIR = "tests/e2e"


@dataclass(frozen=True)
class _TestFunction:
    file: str
    function_name: str
    lineno: int
    source: str


def compute_assertion_strength(
    *,
    project_root: Path,
    change_id: str,
    batch_id: str = ASSERTION_STRENGTH_BATCH_ID,
    api_dir: str | None = None,
    e2e_dir: str | None = None,
) -> AssertionStrengthEvidence:
    """Classify api/e2e test functions; evaluate only when both surfaces measure."""
    resolved_api, resolved_e2e = _resolve_test_dirs(project_root, api_dir=api_dir, e2e_dir=e2e_dir)
    api_tests = _discover_tests(project_root, resolved_api)
    e2e_tests = _discover_tests(project_root, resolved_e2e)

    api_slice, api_weak = _classify_surface("api", api_tests)
    e2e_slice, e2e_weak = _classify_surface("e2e", e2e_tests)

    source = {
        "api_dir": resolved_api,
        "e2e_dir": resolved_e2e,
        "pr_metrics_rel": PR_METRICS_REL,
        "classifier": "assurance_agent.verification.assertion_class",
    }

    if api_slice is None or e2e_slice is None:
        detail = _pending_detail(api_slice is not None, e2e_slice is not None)
        return AssertionStrengthEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            declared=None,
            surfaces=(),
            weak_assertions=(),
            shortboards=(
                MetricShortboard(
                    code="pending_nightly",
                    metric="assertion_strength",
                    detail=detail,
                ),
            ),
            source=source,
        )

    declared = MetricScope.of(
        total=api_slice.declared.total + e2e_slice.declared.total,
        covered=api_slice.declared.covered + e2e_slice.declared.covered,
        uncovered=tuple(api_slice.declared.uncovered) + tuple(e2e_slice.declared.uncovered),
    )
    assert declared.value is not None
    weak = tuple(sorted(api_weak + e2e_weak, key=lambda row: (row.surface, row.locator)))
    return AssertionStrengthEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="evaluated",
        value=declared.value,
        declared=declared,
        surfaces=(api_slice, e2e_slice),
        weak_assertions=weak,
        source=source,
    )


def compute_assertion_strength_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Side-effecting op: scan tests, classify, write nightly assertion-strength.json."""
    del task
    change_id = context.change_id or workspace.change_dir.name
    evidence = compute_assertion_strength(
        project_root=workspace.project_root,
        change_id=change_id,
    )
    out = workspace.change_dir / ASSERTION_STRENGTH_EVIDENCE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(evidence))
    return TaskResult(
        status="succeeded",
        value={
            "path": ASSERTION_STRENGTH_EVIDENCE_REL,
            "status": evidence.status,
            "value": evidence.value,
            "weak_assertions": len(evidence.weak_assertions),
        },
    )


def _resolve_test_dirs(
    project_root: Path,
    *,
    api_dir: str | None,
    e2e_dir: str | None,
) -> tuple[str, str]:
    if api_dir is not None and e2e_dir is not None:
        return api_dir, e2e_dir
    try:
        cfg = load_config(project_root)
        return (
            api_dir or _strip_dot_slash(cfg.tests.api) or _DEFAULT_API_DIR,
            e2e_dir or _strip_dot_slash(cfg.tests.e2e) or _DEFAULT_E2E_DIR,
        )
    except Exception:  # noqa: BLE001 — fall back to packaged defaults
        return api_dir or _DEFAULT_API_DIR, e2e_dir or _DEFAULT_E2E_DIR


def _strip_dot_slash(value: str) -> str:
    return value[2:] if value.startswith("./") else value


def _discover_tests(project_root: Path, rel_dir: str) -> tuple[_TestFunction, ...]:
    root = project_root / rel_dir
    if not root.is_dir():
        return ()
    found: list[_TestFunction] = []
    for path in sorted(root.rglob("test_*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rel = path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        found.extend(_test_functions_in(source, file=rel))
    return tuple(found)


def _test_functions_in(source: str, *, file: str) -> list[_TestFunction]:
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError:
        return []
    out: list[_TestFunction] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            out.append(_TestFunction(file=file, function_name=node.name, lineno=node.lineno, source=source))
            continue
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                    "test_"
                ):
                    out.append(
                        _TestFunction(
                            file=file,
                            function_name=item.name,
                            lineno=item.lineno,
                            source=source,
                        )
                    )
    return out


def _classify_surface(
    layer: AssertionStrengthSurfaceLayer,
    tests: Sequence[_TestFunction],
) -> tuple[AssertionStrengthSurfaceSlice | None, list[WeakAssertionLocator]]:
    if not tests:
        return None, []
    classify = classify_api_assertions if layer == "api" else classify_e2e_assertions
    strong = 0
    weak_rows: list[WeakAssertionLocator] = []
    for test in tests:
        result = classify(test.source, function_name=test.function_name)
        locator = f"{test.file}::{test.function_name}"
        if result.strength == "strong":
            strong += 1
        else:
            weak_rows.append(
                WeakAssertionLocator(
                    locator=locator,
                    surface=layer,
                    file=test.file,
                    function_name=test.function_name,
                    lineno=test.lineno,
                    reasons=result.reasons,
                )
            )
    total = len(tests)
    uncovered = tuple(sorted(row.locator for row in weak_rows))
    declared = MetricScope.of(total=total, covered=strong, uncovered=uncovered)
    assert declared.value is not None
    return (
        AssertionStrengthSurfaceSlice(
            layer=layer,
            declared=declared,
            value=declared.value,
            strong=strong,
            weak=total - strong,
        ),
        weak_rows,
    )


def _pending_detail(api_ok: bool, e2e_ok: bool) -> str:
    missing: list[str] = []
    if not api_ok:
        missing.append("api")
    if not e2e_ok:
        missing.append("e2e")
    return "surface not measurable: " + ", ".join(missing)


__all__ = [
    "ASSERTION_STRENGTH_BATCH_ID",
    "ASSERTION_STRENGTH_EVIDENCE_REL",
    "compute_assertion_strength",
    "compute_assertion_strength_operation",
]
