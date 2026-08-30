"""Normalize a confined pytest json-report into file-level results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from assurance_execution.contracts.execution import ExecutionStatus, RawTestResultV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.common import OutputError

_OUTCOME_MAP: dict[str, ExecutionStatus] = {
    "passed": "passed",
    "xpassed": "passed",
    "failed": "failed",
    "error": "failed",
    "skipped": "skipped",
    "xfailed": "skipped",
}
_CASE_ID_RE = re.compile(
    r"(?<![A-Z0-9])(TC[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*[-_][0-9]{3})(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_OUTSIDE_REASON = "execution evidence contains a test outside the closed mapping"


def extract_case_id(text: str) -> str:
    match = _CASE_ID_RE.search(text)
    return match.group(1).upper().replace("-", "_") if match else ""


def test_file_from_nodeid(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _selected_for_nodeid(nodeid: str, allowed: frozenset[str]) -> str | None:
    file_path = test_file_from_nodeid(nodeid)
    if file_path in allowed:
        return file_path
    matches = tuple(
        selector for selector in allowed if nodeid == selector or nodeid.startswith(f"{selector}[")
    )
    return matches[0] if len(matches) == 1 else None


def _longrepr_text(longrepr: object) -> str:
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, Mapping):
        crash = longrepr.get("crash")
        if isinstance(crash, Mapping) and crash.get("message"):
            return str(crash["message"])
        if longrepr.get("message"):
            return str(longrepr["message"])
    return ""


def _duration_ms(test: Mapping[str, Any]) -> int:
    duration = 0.0
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if isinstance(phase, Mapping):
            duration += float(phase.get("duration", 0.0) or 0.0)
    return max(0, round(duration * 1000))


def _message(test: Mapping[str, Any]) -> str:
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if isinstance(phase, Mapping) and phase.get("outcome") in {"failed", "error"}:
            text = _longrepr_text(phase.get("longrepr"))
            if text:
                return text
    return _longrepr_text(test.get("longrepr"))


def _rank(status: ExecutionStatus) -> int:
    return {"failed": 2, "skipped": 1, "passed": 0}[status]


def parse_pytest_report(
    report: Mapping[str, Any],
    mapping: ClosedMappingV1,
) -> tuple[RawTestResultV1, ...]:
    allowed = frozenset(mapping.selected)
    case_by_test = {entry.test: entry.case_id for entry in mapping.mappings}
    aggregated: dict[str, dict[str, Any]] = {}
    tests = report.get("tests")
    if tests is None:
        tests = ()
    if not isinstance(tests, list | tuple):
        raise OutputError("pytest report tests must be a list")
    for item in tests:
        if not isinstance(item, Mapping):
            raise OutputError("pytest report test row must be an object")
        nodeid = str(item.get("nodeid") or "")
        path = _selected_for_nodeid(nodeid, allowed)
        if path is None:
            raise OutputError(_OUTSIDE_REASON)
        status = _OUTCOME_MAP.get(str(item.get("outcome") or ""), "failed")
        current = aggregated.get(path)
        if current is None or _rank(status) > _rank(current["status"]):
            aggregated[path] = {
                "test": path,
                "status": status,
                "duration_ms": _duration_ms(item),
                "message": _message(item),
                "case_id": case_by_test.get(path) or extract_case_id(nodeid) or None,
            }
        else:
            current["duration_ms"] = int(current["duration_ms"]) + _duration_ms(item)
            if not current["message"]:
                current["message"] = _message(item)
    missing = [path for path in mapping.selected if path not in aggregated]
    if missing:
        raise OutputError("execution evidence must uniquely cover the closed mapping")
    return tuple(
        RawTestResultV1.model_validate(aggregated[path]) for path in mapping.selected if path in aggregated
    )


def receipt_counts(
    results: tuple[RawTestResultV1, ...],
    *,
    exit_code: int,
    collected: int | None = None,
) -> dict[str, int]:
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    skipped = sum(1 for item in results if item.status == "skipped")
    return {
        "exit_code": exit_code,
        "collected": collected if collected is not None else len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }
