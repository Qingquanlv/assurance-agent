"""Pure confirm_escape_analysis transform — human_confirmed block on Problem."""

from __future__ import annotations

from assurance_agent.artifacts.models.issues import EscapeAnalysis, Problem
from assurance_agent.workflow.issues.escape import confirm_escape_analysis
from tests.unit.artifacts.test_models_issues import make_problem


def test_confirm_escape_analysis_returns_human_confirmed_problem() -> None:
    problem = Problem.model_validate(
        make_problem(
            escape_analysis={
                "is_escape": True,
                "authority": "llm_provisional",
                "rationale": "draft",
                "draft_classification": {
                    "is_escape": True,
                    "authority": "llm_provisional",
                    "rationale": "llm draft",
                },
            }
        )
    )
    confirmed = confirm_escape_analysis(
        problem,
        is_escape=True,
        confirmed_by="qa-lead",
        confirmed_at="2026-08-05T15:00:00Z",
        missed_oracle_ids=("ORACLE-1",),
        missed_obligation_ids=("entities.dept.constraints.name_unique",),
        rationale="confirmed after triage",
    )
    assert isinstance(confirmed, Problem)
    assert confirmed is not problem
    assert confirmed.escape_analysis is not None
    assert confirmed.escape_analysis == EscapeAnalysis.model_validate(
        {
            "is_escape": True,
            "authority": "human_confirmed",
            "confirmed_at": "2026-08-05T15:00:00Z",
            "confirmed_by": "qa-lead",
            "missed_oracle_ids": ("ORACLE-1",),
            "missed_obligation_ids": ("entities.dept.constraints.name_unique",),
            "rationale": "confirmed after triage",
            "draft_classification": {
                "is_escape": True,
                "authority": "llm_provisional",
                "rationale": "llm draft",
            },
        }
    )


def test_confirm_escape_analysis_not_escape() -> None:
    problem = Problem.model_validate(make_problem())
    confirmed = confirm_escape_analysis(
        problem,
        is_escape=False,
        confirmed_by="reviewer",
        confirmed_at="2026-08-05T15:00:00Z",
    )
    assert confirmed.escape_analysis is not None
    assert confirmed.escape_analysis.is_escape is False
    assert confirmed.escape_analysis.authority == "human_confirmed"
