"""Shared check context, evidence construction, and Markdown table parsing.

Checks only decide facts and produce locatable evidence; policy decides the
corresponding action.  This module must not import concrete checks: dependencies
flow from base to checks to registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding


@dataclass(frozen=True)
class CheckContext:
    plan_texts: Mapping[str, str]
    cases: Sequence[Mapping[str, object]]
    data_knowledge: Mapping[str, object]
    layer: str = "api"
    # From review/{layer}-plan-review.json when present (mechanical runs before
    # first review: empty → capability_keys is inert until a review exists).
    required_capabilities: Sequence[str] = ()


CheckFn = Callable[[CheckContext], CheckEvidence]


def evidence(check_id: str, findings: Sequence[Finding], refs: Sequence[str]) -> CheckEvidence:
    ordered = tuple(findings)
    return CheckEvidence(
        check_id=check_id,
        status="fail" if ordered else "pass",
        findings=ordered,
        refs=tuple(sorted(set(refs))),
    )


def table_rows(text: str) -> Iterator[tuple[int, list[str]]]:
    """Yield Markdown table rows as (line number, unwrapped cells), sans separators."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _cells(stripped)
        if _is_separator(cells):
            continue
        yield lineno, cells


def _cells(stripped_line: str) -> list[str]:
    return [cell.strip().strip("`").replace("**", "").strip() for cell in stripped_line.strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":"} and cell for cell in cells)


def case_id_rows(text: str) -> Iterator[tuple[int, int, str, str]]:
    """Yield Case ID-table rows as (table index, line number, ID cell, row text)."""
    table_index = -1
    case_column: int | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            case_column = None
            continue
        cells = _cells(stripped)
        if _is_separator(cells):
            continue
        lowered = [cell.lower() for cell in cells]
        if "case id" in lowered:
            table_index += 1
            case_column = lowered.index("case id")
            continue
        if case_column is None or len(cells) <= case_column:
            continue
        yield table_index, lineno, cells[case_column], " ".join(cells)
