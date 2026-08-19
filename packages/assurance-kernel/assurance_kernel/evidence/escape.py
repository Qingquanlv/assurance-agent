"""C1 escape-rate feedstock (pure counters; MetricKey / nightly deferred to M4).

逃逸率（escape rate）semantics for this module:

- **Numerator** (``escapes``): Problems whose ``escape_analysis`` is
  ``authority=human_confirmed`` and ``is_escape=True``.
- **Denominator** (``analyzed``): Problems whose ``escape_analysis`` is
  ``authority=human_confirmed`` with a decided ``is_escape`` (True **or** False).
  Rate = escapes / analyzed among human-confirmed classifications.
- **Excluded**: missing ``escape_analysis``, and any ``llm_provisional`` draft
  (top-level or ``draft_classification`` only). LLM drafts never inflate C1.

Zero analyzed → ``rate is None`` (never ``0/0 = 1.0``). Long-term / report-only;
not a single-change verdict input. M4 will fold these counters into MetricsDocument.
"""

from __future__ import annotations

from collections.abc import Sequence

from assurance_kernel.artifacts.models.issues import Problem

__all__ = [
    "compute_escape_rate",
    "count_confirmed_escapes",
    "count_escape_denominator",
    "is_confirmed_escape",
    "list_confirmed_escape_ids",
]


def _is_human_confirmed_analysis(problem: Problem) -> bool:
    analysis = problem.escape_analysis
    return analysis is not None and analysis.authority == "human_confirmed" and analysis.is_escape is not None


def is_confirmed_escape(problem: Problem) -> bool:
    """True only for human-confirmed ``is_escape=True`` (C1 numerator atom)."""
    analysis = problem.escape_analysis
    return analysis is not None and analysis.authority == "human_confirmed" and analysis.is_escape is True


def count_confirmed_escapes(problems: Sequence[Problem]) -> int:
    """Count human-confirmed escapes (C1 numerator feedstock)."""
    return sum(1 for problem in problems if is_confirmed_escape(problem))


def list_confirmed_escape_ids(problems: Sequence[Problem]) -> tuple[str, ...]:
    """Stable-order ``problem_id`` list of confirmed escapes (input order)."""
    return tuple(problem.problem_id for problem in problems if is_confirmed_escape(problem))


def count_escape_denominator(problems: Sequence[Problem]) -> int:
    """Count human-confirmed escape analyses (escape or not) — C1 denominator.

    Matches 逃逸率 = confirmed_escapes / human-confirmed analyses.
    """
    return sum(1 for problem in problems if _is_human_confirmed_analysis(problem))


def compute_escape_rate(
    problems: Sequence[Problem],
) -> tuple[int, int, float | None]:
    """Pure C1 rate: ``(escapes, analyzed, rate|None)``.

    Zero analyzed → ``rate is None`` (not_evaluated); never vacuous 1.0.
    """
    escapes = count_confirmed_escapes(problems)
    analyzed = count_escape_denominator(problems)
    if analyzed == 0:
        return escapes, 0, None
    return escapes, analyzed, escapes / analyzed
