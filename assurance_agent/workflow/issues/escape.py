"""Pure transform: attach human-confirmed EscapeAnalysis onto a Problem.

Does not write the project ledger — callers persist via existing store/review
paths. Preserves any prior ``draft_classification`` from an LLM provisional block.
"""

from __future__ import annotations

from collections.abc import Sequence

from assurance_agent.artifacts.models.issues import EscapeAnalysis, Problem

__all__ = ["confirm_escape_analysis"]


def confirm_escape_analysis(
    problem: Problem,
    *,
    is_escape: bool,
    confirmed_by: str,
    confirmed_at: str,
    missed_oracle_ids: Sequence[str] = (),
    missed_obligation_ids: Sequence[str] = (),
    rationale: str | None = None,
) -> Problem:
    """Return a new frozen Problem with ``authority=human_confirmed`` escape block."""
    prior = problem.escape_analysis
    draft = prior.draft_classification if prior is not None else None
    analysis = EscapeAnalysis(
        is_escape=is_escape,
        authority="human_confirmed",
        confirmed_at=confirmed_at,
        confirmed_by=confirmed_by,
        missed_oracle_ids=tuple(missed_oracle_ids),
        missed_obligation_ids=tuple(missed_obligation_ids),
        rationale=rationale,
        draft_classification=draft,
    )
    return problem.model_copy(update={"escape_analysis": analysis})
