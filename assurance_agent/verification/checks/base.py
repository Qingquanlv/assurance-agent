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
        cells = [cell.strip().strip("`").replace("**", "").strip() for cell in stripped.strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        yield lineno, cells
