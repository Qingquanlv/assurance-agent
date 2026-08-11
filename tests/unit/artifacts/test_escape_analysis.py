"""EscapeAnalysis schema on Problem — additive optional, human-confirmed invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import (
    EscapeAnalysis,
    EscapeDraftClassification,
    Problem,
)
from tests.unit.artifacts.test_models_issues import make_problem


def test_old_problem_without_escape_analysis_still_parses() -> None:
    model = Problem.model_validate(make_problem())
    assert model.escape_analysis is None


def test_escape_analysis_human_confirmed_escape_parses() -> None:
    model = Problem.model_validate(
        make_problem(
            escape_analysis={
                "is_escape": True,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "qa-lead",
                "missed_oracle_ids": ("ORACLE-unique-500",),
                "missed_obligation_ids": ("entities.dept.constraints.name_unique",),
                "rationale": "validation gap shipped past nightly",
            }
        )
    )
    assert model.escape_analysis is not None
    assert model.escape_analysis.is_escape is True
    assert model.escape_analysis.authority == "human_confirmed"
    assert model.escape_analysis.missed_oracle_ids == ("ORACLE-unique-500",)


def test_escape_analysis_human_confirmed_requires_confirmed_at() -> None:
    with pytest.raises(ValidationError, match="confirmed_at"):
        EscapeAnalysis.model_validate(
            {
                "is_escape": True,
                "authority": "human_confirmed",
                "confirmed_by": "qa-lead",
            }
        )


def test_escape_analysis_human_confirmed_requires_is_escape() -> None:
    with pytest.raises(ValidationError, match="is_escape"):
        EscapeAnalysis.model_validate(
            {
                "is_escape": None,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "qa-lead",
            }
        )


def test_escape_analysis_llm_provisional_draft_allowed_without_confirmed_at() -> None:
    analysis = EscapeAnalysis.model_validate(
        {
            "is_escape": True,
            "authority": "llm_provisional",
            "rationale": "looks like an escape",
            "draft_classification": {
                "is_escape": True,
                "authority": "llm_provisional",
                "rationale": "draft note",
            },
        }
    )
    assert analysis.authority == "llm_provisional"
    assert analysis.confirmed_at is None
    assert isinstance(analysis.draft_classification, EscapeDraftClassification)


def test_problem_projection_carries_escape_analysis() -> None:
    from assurance_agent.artifacts.models.issues import ProblemProjection

    model = ProblemProjection.model_validate(
        {
            "schema_version": "1.0",
            "generated_at": "2026-08-05T12:00:00Z",
            "problems": [
                make_problem(
                    escape_analysis={
                        "is_escape": False,
                        "authority": "human_confirmed",
                        "confirmed_at": "2026-08-05T12:00:00Z",
                        "confirmed_by": "reviewer",
                    }
                )
            ],
        }
    )
    assert model.problems[0].escape_analysis is not None
    assert model.problems[0].escape_analysis.is_escape is False
