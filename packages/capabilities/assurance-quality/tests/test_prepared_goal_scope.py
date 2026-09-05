from assurance_intake.contracts.quality_goals import PreparedObligationV1
from assurance_quality.operations.goal_scope import goal_case_map


def test_unmapped_prepared_journey_remains_in_denominator() -> None:
    rows = (
        PreparedObligationV1(
            mrc_id="MRC-001",
            key="checkout",
            category="e2e",
            required=True,
            layer="e2e",
        ),
    )
    assert goal_case_map("journey_coverage", baseline=rows, reviewed={}) == {"checkout": frozenset()}


def test_many_cases_do_not_multiply_one_journey() -> None:
    scope = goal_case_map(
        "journey_coverage",
        baseline=(),
        reviewed={"checkout": ("TC_1", "TC_2")},
    )
    assert scope == {"checkout": frozenset({"TC_1", "TC_2"})}
