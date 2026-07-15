"""Normalise a pytest-json-report file into a TargetResult.

Only reads what the real test runner wrote; a missing or corrupt report yields
a SKIPPED result carrying the reason (never a fabricated pass).
"""
import json
from pathlib import Path
from typing import Any

from assurance_agent.workflow.execution.case_id import extract_case_id
from assurance_agent.workflow.execution.results import (
    CaseResult,
    ExecutionStatus,
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
        return _skipped(change_id, batch_id, target, command, source,
                        "pytest json report not found — pytest may not have run.")
    try:
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return _skipped(change_id, batch_id, target, command, source,
                        f"failed to parse pytest json report: {err}")

    cases: list[CaseResult] = []
    unmapped: list[CaseResult] = []
    for test in report.get("tests", []):
        entry = _to_case(test, raw_log_path)
        (cases if entry.case_id else unmapped).append(entry)

    all_cases = cases + unmapped
    passed = sum(1 for c in all_cases if c.status == "passed")
    failed = sum(1 for c in all_cases if c.status == "failed")
    skipped = sum(1 for c in all_cases if c.status == "skipped")

    if not all_cases:
        status: ExecutionStatus = "skipped"
    elif failed > 0:
        status = "failed"
    else:
        status = "passed"

    return TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target=target,
        status=status,
        command=command,
        source=source,
        total=len(all_cases),
        passed=passed,
        failed=failed,
        skipped=skipped,
        cases=cases,
        unmapped_tests=unmapped,
    )


def _to_case(test: dict[str, Any], raw_log_path: str) -> CaseResult:
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
        case_id=extract_case_id(nodeid),
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
        case_id="", status="skipped", file="", test_name=reason,
        duration_ms=0, message=reason, raw_log_ref=source.raw_log,
    )
    return TargetResult(
        change_id=change_id, batch_id=batch_id, target=target, status="skipped",
        command=command, source=source, total=0, passed=0, failed=0, skipped=0,
        cases=[], unmapped_tests=[placeholder],
    )
