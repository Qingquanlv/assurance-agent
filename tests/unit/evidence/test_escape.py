"""C1 escape feedstock counters — pure helpers, not MetricKey."""

from __future__ import annotations

from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.evidence.escape import (
    compute_escape_rate,
    count_confirmed_escapes,
    count_escape_denominator,
    is_confirmed_escape,
    list_confirmed_escape_ids,
)
from tests.unit.artifacts.test_models_issues import make_problem


def _problem(**escape_overrides: object) -> Problem:
    return Problem.model_validate(make_problem(escape_analysis=dict(escape_overrides)))


def test_llm_provisional_draft_not_counted() -> None:
    problem = _problem(
        is_escape=True,
        authority="llm_provisional",
        rationale="provisional only",
    )
    assert is_confirmed_escape(problem) is False
    assert count_confirmed_escapes([problem]) == 0
    assert count_escape_denominator([problem]) == 0
    escapes, analyzed, rate = compute_escape_rate([problem])
    assert (escapes, analyzed, rate) == (0, 0, None)


def test_human_confirmed_escape_counted() -> None:
    problem = _problem(
        is_escape=True,
        authority="human_confirmed",
        confirmed_at="2026-08-05T12:00:00Z",
        confirmed_by="qa-lead",
    )
    assert is_confirmed_escape(problem) is True
    assert count_confirmed_escapes([problem]) == 1
    assert list_confirmed_escape_ids([problem]) == ("PROB-ghi789",)
    assert count_escape_denominator([problem]) == 1


def test_human_confirmed_not_escape_increments_analyzed_not_escapes() -> None:
    problem = _problem(
        is_escape=False,
        authority="human_confirmed",
        confirmed_at="2026-08-05T12:00:00Z",
        confirmed_by="qa-lead",
    )
    assert is_confirmed_escape(problem) is False
    assert count_confirmed_escapes([problem]) == 0
    assert count_escape_denominator([problem]) == 1
    escapes, analyzed, rate = compute_escape_rate([problem])
    assert escapes == 0
    assert analyzed == 1
    assert rate == 0.0


def test_rate_none_when_none_analyzed() -> None:
    bare = Problem.model_validate(make_problem())
    escapes, analyzed, rate = compute_escape_rate([bare])
    assert escapes == 0
    assert analyzed == 0
    assert rate is None


def test_escape_rate_mix() -> None:
    escape = _problem(
        is_escape=True,
        authority="human_confirmed",
        confirmed_at="2026-08-05T12:00:00Z",
        confirmed_by="a",
    )
    not_escape = Problem.model_validate(
        make_problem(
            problem_id="PROB-other",
            escape_analysis={
                "is_escape": False,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "b",
            },
        )
    )
    draft = Problem.model_validate(
        make_problem(
            problem_id="PROB-draft",
            escape_analysis={
                "is_escape": True,
                "authority": "llm_provisional",
            },
        )
    )
    escapes, analyzed, rate = compute_escape_rate([escape, not_escape, draft])
    assert escapes == 1
    assert analyzed == 2
    assert rate == 0.5
