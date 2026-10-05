from __future__ import annotations


import pytest


from pydantic import ValidationError

from assurance_quality.contracts.coverage import (
    MinimumCoverageMatrix,
)


def test_minimum_coverage_matrix_requires_structured_rows() -> None:
    with pytest.raises(ValidationError):
        MinimumCoverageMatrix.model_validate(
            [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "menus.create",
                    "required": True,
                    "covered_by_cases": ["TC_A"],
                    "status": "covered",
                }
            ]
        )


def test_coverage_states_are_exactly_the_closed_set() -> None:
    from assurance_quality.contracts.coverage import COVERAGE_STATES
    from assurance_quality.contracts.decisions import CoverageAssessmentPublicV1

    assert set(COVERAGE_STATES) == {
        "satisfied",
        "repair_required",
        "exhausted",
        "needs_human",
        "inconclusive",
    }
    assert len(COVERAGE_STATES) == 5
    assert CoverageAssessmentPublicV1.model_fields["coverage_state"].annotation is not None
