"""Normalise a pytest-json-report file into a TargetResult.

Only reads what the real test runner wrote; a missing or corrupt report yields
a SKIPPED result carrying the reason (never a fabricated pass).

Identity routing (§5.1): case_id first → ``cases``; else
``@pytest.mark.property(...)`` → ``property_tests``; else ``unmapped_tests``.
The three buckets are mutually exclusive per executed test.
"""

import ast
import json
from pathlib import Path
from typing import Any

from assurance_agent.evidence.case_id import extract_case_id
from assurance_agent.workflow.execution.results import (
    CaseResult,
    ExecutionStatus,
    PropertyTestResult,
    PytestTarget,
    ResultSource,
    TargetResult,
)

_OUTCOME_MAP: dict[str, ExecutionStatus] = {
    "passed": "passed",
    "xpassed": "passed",
    "failed": "failed",
    "error": "failed",
    "skipped": "skipped",
    "xfailed": "skipped",
}


def parse_pytest_json(
    *,
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    report_path: Path,
    raw_log_path: str,
    command: str,
) -> TargetResult:
    source = ResultSource(framework="pytest", raw_log=raw_log_path, report_json=str(report_path))

    if not report_path.is_file():
        return _skipped(
            change_id,
            batch_id,
            target,
            command,
            source,
            "pytest json report not found — pytest may not have run.",
        )
    try:
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return _skipped(
            change_id, batch_id, target, command, source, f"failed to parse pytest json report: {err}"
        )

    cases: list[CaseResult] = []
    property_tests: list[PropertyTestResult] = []
    unmapped: list[CaseResult] = []
    for test in report.get("tests", []):
        case_id = extract_case_id(str(test.get("nodeid", "")))
        if case_id:
            cases.append(_to_case(test, raw_log_path, case_id=case_id))
            continue
        constraint_keys = _property_constraint_keys(test)
        if constraint_keys is not None:
            property_tests.append(_to_property(test, batch_id=batch_id, constraint_keys=constraint_keys))
            continue
        unmapped.append(_to_case(test, raw_log_path, case_id=""))

    outcomes: list[ExecutionStatus] = [
        *(c.status for c in cases),
        *(p.outcome for p in property_tests),
        *(u.status for u in unmapped),
    ]

    if not outcomes:
        # No per-test results. Distinguish a benign "nothing ran" from a session
        # that was ABORTED before producing results. pytest exit codes: 0 ok,
        # 1 tests failed, 2 interrupted, 3 internal error, 4 usage error,
        # 5 no tests collected. Only a clean / no-tests-collected exit (or a
        # report without an exitcode, e.g. legacy fixtures) is a benign skip.
        # A non-zero abort with tests COLLECTED — e.g. a conftest `pytest.exit()`
        # against an unready SUT (HTTP 502), or a collection-time import error —
        # must surface as FAILED, not be masked as a benign SKIPPED that hides
        # that the suite never actually ran.
        exitcode = report.get("exitcode")
        if exitcode not in (None, 0, 5):
            summary = report.get("summary") or {}
            collected = int(summary.get("collected", 0) or 0)
            reason = (
                f"pytest session aborted before producing per-test results "
                f"(exitcode={exitcode}, collected={collected}) — e.g. a conftest "
                f"pytest.exit() on an unready SUT or a collection error; see raw log."
            )
            return _aborted(change_id, batch_id, target, command, source, reason)

    passed = sum(1 for status in outcomes if status == "passed")
    failed = sum(1 for status in outcomes if status == "failed")
    skipped = sum(1 for status in outcomes if status == "skipped")

    if not outcomes:
        status: ExecutionStatus = "skipped"
    elif failed > 0:
        status = "failed"
    elif passed == 0:
        # A report containing only skipped tests proves that collection worked,
        # not that the selected target executed successfully.
        status = "skipped"
    else:
        status = "passed"

    return TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target=target,
        status=status,
        command=command,
        source=source,
        total=len(outcomes),
        passed=passed,
        failed=failed,
        skipped=skipped,
        cases=cases,
        unmapped_tests=unmapped,
        property_tests=property_tests,
    )


def _property_constraint_keys(test: dict[str, Any]) -> tuple[str, ...] | None:
    """Return marker args when this test is a property test; else None.

    ``None`` means "not a property test". An empty tuple means the property
    marker was present without extractable string args (still routes to
    ``property_tests``). Keywords-only identity carries no args — keys stay
    empty by design; prefer dict markers or string ``property("k", ...)`` forms
    when constraint keys are required.
    """
    keys: list[str] = []
    found = False

    markers = test.get("markers")
    if isinstance(markers, list):
        for marker in markers:
            if isinstance(marker, dict) and marker.get("name") == "property":
                found = True
                args = marker.get("args") or ()
                if isinstance(args, (list, tuple)):
                    keys.extend(str(arg) for arg in args if isinstance(arg, (str, int, float)))
            elif isinstance(marker, str):
                if marker == "property" or marker.startswith("property(") or marker.startswith("property["):
                    found = True
                    keys.extend(_parse_property_marker_string_args(marker))

    if not found:
        keywords = test.get("keywords")
        if isinstance(keywords, dict) and "property" in keywords:
            found = True
        elif isinstance(keywords, list) and "property" in keywords:
            found = True

    if not found:
        return None
    return tuple(keys)


def _parse_property_marker_string_args(marker: str) -> tuple[str, ...]:
    """Best-effort parse of ``property("k1", 'k2')`` / ``property[k]`` string forms."""
    if marker.startswith("property(") and marker.endswith(")"):
        inner = marker[len("property(") : -1].strip()
        if not inner:
            return ()
        try:
            # Reuse Python literal list parsing for quoted string args.
            parsed = ast.literal_eval(f"[{inner}]")
        except (SyntaxError, ValueError):
            return ()
        if isinstance(parsed, list):
            return tuple(str(item) for item in parsed if isinstance(item, (str, int, float)))
        return ()
    if marker.startswith("property[") and marker.endswith("]"):
        inner = marker[len("property[") : -1].strip()
        if not inner:
            return ()
        # Single unquoted / quoted key in bracket form.
        try:
            parsed = ast.literal_eval(inner)
        except (SyntaxError, ValueError):
            return (inner,) if inner else ()
        if isinstance(parsed, (str, int, float)):
            return (str(parsed),)
        return ()
    return ()


def _to_property(
    test: dict[str, Any],
    *,
    batch_id: str,
    constraint_keys: tuple[str, ...],
) -> PropertyTestResult:
    nodeid = str(test.get("nodeid", ""))
    outcome = _OUTCOME_MAP.get(str(test.get("outcome", "")), "failed")
    file = nodeid.split("::", 1)[0]
    return PropertyTestResult(
        nodeid=nodeid,
        file=file,
        constraint_keys=constraint_keys,
        outcome=outcome,
        batch_id=batch_id,
    )


def _to_case(test: dict[str, Any], raw_log_path: str, *, case_id: str) -> CaseResult:
    nodeid = str(test.get("nodeid", ""))
    outcome = _OUTCOME_MAP.get(str(test.get("outcome", "")), "failed")
    file = nodeid.split("::", 1)[0]
    test_name = nodeid.split("::")[-1] if "::" in nodeid else nodeid

    message = ""
    duration = 0.0
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if not isinstance(phase, dict):
            continue
        duration += float(phase.get("duration", 0.0) or 0.0)
        if not message and phase.get("outcome") in ("failed", "error"):
            message = _longrepr_text(phase.get("longrepr"))

    return CaseResult(
        case_id=case_id,
        status=outcome,
        file=file,
        test_name=test_name,
        duration_ms=round(duration * 1000),
        message=message,
        raw_log_ref=raw_log_path,
    )


def _longrepr_text(longrepr: Any) -> str:
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, dict):
        crash = longrepr.get("crash")
        if isinstance(crash, dict) and crash.get("message"):
            return str(crash["message"])
        if longrepr.get("message"):
            return str(longrepr["message"])
    return ""


def _skipped(
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    command: str,
    source: ResultSource,
    reason: str,
) -> TargetResult:
    placeholder = CaseResult(
        case_id="",
        status="skipped",
        file="",
        test_name=reason,
        duration_ms=0,
        message=reason,
        raw_log_ref=source.raw_log,
    )
    return TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target=target,
        status="skipped",
        command=command,
        source=source,
        total=0,
        passed=0,
        failed=0,
        skipped=0,
        cases=[],
        unmapped_tests=[placeholder],
        property_tests=[],
    )


def _aborted(
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    command: str,
    source: ResultSource,
    reason: str,
) -> TargetResult:
    """Session collected tests but aborted before producing per-test results.

    Reported as a single synthetic FAILED case so the failure surfaces through
    inspect/report/archive rather than being hidden as a benign SKIPPED.
    """
    placeholder = CaseResult(
        case_id="",
        status="failed",
        file="",
        test_name=reason,
        duration_ms=0,
        message=reason,
        raw_log_ref=source.raw_log,
    )
    return TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target=target,
        status="failed",
        command=command,
        source=source,
        total=1,
        passed=0,
        failed=1,
        skipped=0,
        cases=[],
        unmapped_tests=[placeholder],
        property_tests=[],
    )
