"""Check that assert_ideal expectations are not narrowed or dropped."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_agent.verification.checks.base import CheckContext, case_id_rows, evidence

CHECK_ID = "assert_ideal"
_MARKER = "assert_ideal"
_REJECTION = re.compile(r"\b(4xx|400|401|403|404|409|422)\b")
_SERVER_ERROR = re.compile(r"\b(5xx|500|502|503)\b")
_PREFIX_NEGATION = (
    "not ",
    "no ",
    "never ",
    "avoid ",
    "without ",
    "非",
    "不",
    "禁止",
    "避免",
    "不得",
    "不能",
    "不可",
)
_SUFFIX_NEGATION = re.compile(
    r"^\s*(?:(?:is|are|be)\s+)?(?:not|no|never)\b|^\s*(?:非|不|禁止|避免|不得|不能|不可|并非)"
)
_CASE_TEXT_KEYS = ("title", "objective", "summary")
_METRIC_CUE = re.compile(r"p\d+|latency|duration|elapsed|\bms\b|毫秒|耗时|延迟|[<>]=?", re.IGNORECASE)


def _is_negated(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 12) : start].lower()
    suffix = text[end : end + 24].lower()
    return any(marker in prefix for marker in _PREFIX_NEGATION) or bool(_SUFFIX_NEGATION.match(suffix))


def _has_positive_rejection(text: str) -> bool:
    return any(
        _is_http_status(text, match) and not _is_negated(text, match.start(), match.end())
        for match in _REJECTION.finditer(text)
    )


def _is_http_status(text: str, match: re.Match[str]) -> bool:
    token = match.group(0).lower()
    if token.endswith("xx"):
        return True
    context = text[max(0, match.start() - 24) : match.end() + 24]
    if _METRIC_CUE.search(context):
        return False
    return True


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


def _interrupted_case_rows(text: str, scoped_ids: set[str]) -> Iterator[tuple[int, str]]:
    active_case_table = False
    interrupted_case_table = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            active_case_table = False
            interrupted_case_table = False
            continue
        if not stripped.startswith("|"):
            if active_case_table and stripped:
                active_case_table = False
                interrupted_case_table = True
            continue
        cells = [cell.strip().strip("`") for cell in stripped.strip("|").split("|")]
        if "case id" in {cell.casefold() for cell in cells}:
            active_case_table = True
            interrupted_case_table = False
            continue
        if active_case_table or not interrupted_case_table:
            continue
        matched = scoped_ids.intersection(cells)
        if matched:
            for case_id in sorted(matched):
                yield lineno, case_id
        else:
            interrupted_case_table = False


def check_assert_ideal(ctx: CheckContext) -> CheckEvidence:
    findings: list[Finding] = []
    rows_by_case: dict[str, list[tuple[str, int, int, str]]] = {}
    for rel in sorted(ctx.plan_texts):
        for table_index, lineno, case_id, row in case_id_rows(ctx.plan_texts[rel]):
            rows_by_case.setdefault(case_id, []).append((rel, table_index, lineno, row))

    scoped_ids = {str(entry["case_id"]) for entry in _scoped_cases(ctx)}
    for rel in sorted(ctx.plan_texts):
        for lineno, case_id in _interrupted_case_rows(ctx.plan_texts[rel], scoped_ids):
            findings.append(
                Finding(
                    locator=f"{rel}:{lineno}",
                    actual="Case ID row outside a parseable Case ID table",
                    expected="keep Case ID rows contiguous under a Case ID table header",
                )
            )

    for rel in sorted(ctx.plan_texts):
        for _, lineno, _, row in case_id_rows(ctx.plan_texts[rel]):
            if _MARKER not in row:
                continue
            for match in _SERVER_ERROR.finditer(row):
                if _is_http_status(row, match) and not _is_negated(row, match.start(), match.end()):
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
        if _has_positive_rejection(_case_text(entry)) and not any(
            _has_positive_rejection(row) for *_, row in matched
        ):
            findings.append(
                Finding(
                    locator=case_id,
                    actual="no 4xx-class token in any plan row",
                    expected="keep the case's 4xx-class rejection expectation",
                )
            )
    return evidence(CHECK_ID, findings, tuple(ctx.plan_texts))
