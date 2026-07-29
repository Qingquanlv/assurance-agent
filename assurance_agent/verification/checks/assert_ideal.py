"""Check that assert_ideal expectations are not narrowed or dropped."""

from __future__ import annotations

import re
from collections.abc import Mapping

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_agent.verification.checks.base import CheckContext, case_id_rows, evidence

CHECK_ID = "assert_ideal"
_MARKER = "assert_ideal"
_REJECTION = re.compile(r"\b(4xx|400|401|403|404|409|422)\b")
_SERVER_ERROR = re.compile(r"\b(5xx|500|502|503)\b")
_NEGATION = ("not ", "no ", "never ", "非", "不", "avoid ", "without ")
_CASE_TEXT_KEYS = ("title", "objective", "summary")


def _is_negated(line: str, start: int) -> bool:
    window = line[max(0, start - 12) : start].lower()
    return any(marker in window for marker in _NEGATION)


def _case_text(entry: Mapping[str, object]) -> str:
    parts = [str(entry.get(key, "")) for key in _CASE_TEXT_KEYS]
    assertions = entry.get("assertions")
    if isinstance(assertions, list):
        parts.extend(str(item) for item in assertions)
    return " ".join(parts)


def _in_scope(entry: Mapping[str, object], layer: str) -> bool:
    if str(entry.get("type", "")).lower() != layer.lower():
        return False
    automation = entry.get("automation")
    return isinstance(automation, Mapping) and automation.get("required") is True


def _scoped_cases(ctx: CheckContext) -> list[Mapping[str, object]]:
    entries: list[Mapping[str, object]] = []
    for doc in ctx.cases:
        for bucket in ("added", "modified"):
            values = doc.get(bucket)
            if not isinstance(values, list):
                continue
            entries.extend(
                item
                for item in values
                if isinstance(item, Mapping)
                and isinstance(item.get("case_id"), str)
                and _in_scope(item, ctx.layer)
            )
    return entries


def check_assert_ideal(ctx: CheckContext) -> CheckEvidence:
    findings: list[Finding] = []
    rows_by_case: dict[str, list[tuple[str, int, int, str]]] = {}
    for rel in sorted(ctx.plan_texts):
        for table_index, lineno, case_id, row in case_id_rows(ctx.plan_texts[rel]):
            rows_by_case.setdefault(case_id, []).append((rel, table_index, lineno, row))

    for rel in sorted(ctx.plan_texts):
        for _, lineno, _, row in case_id_rows(ctx.plan_texts[rel]):
            if _MARKER not in row:
                continue
            for match in _SERVER_ERROR.finditer(row):
                if not _is_negated(row, match.start()):
                    findings.append(
                        Finding(
                            locator=f"{rel}:{lineno}",
                            actual=match.group(0),
                            expected="a 4xx-class rejection; assert_ideal must not expect a server error",
                        )
                    )

    for entry in _scoped_cases(ctx):
        case_id = str(entry["case_id"])
        matched = rows_by_case.get(case_id, [])
        if not matched:
            findings.append(
                Finding(
                    locator=case_id,
                    actual="no plan row",
                    expected=f"an in-scope {ctx.layer} case must appear in a Case ID column",
                )
            )
            continue
        seen: set[tuple[str, int]] = set()
        for rel, table_index, lineno, _ in matched:
            key = (rel, table_index)
            if key in seen:
                findings.append(
                    Finding(
                        locator=f"{rel}:{lineno}",
                        actual=f"duplicate {case_id} in the same table",
                        expected="one row per case per table",
                    )
                )
            seen.add(key)
        if _REJECTION.search(_case_text(entry)) and not any(_REJECTION.search(row) for *_, row in matched):
            findings.append(
                Finding(
                    locator=case_id,
                    actual="no 4xx-class token in any plan row",
                    expected="keep the case's 4xx-class rejection expectation",
                )
            )
    return evidence(CHECK_ID, findings, tuple(ctx.plan_texts))
