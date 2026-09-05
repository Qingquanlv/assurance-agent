"""Merge intake quality obligations with reviewed Case evidence."""

from __future__ import annotations

from collections.abc import Mapping

from assurance_intake.contracts.quality_goals import CoverageGoal, PreparedObligationV1


def goal_case_map(
    goal: CoverageGoal,
    *,
    baseline: tuple[PreparedObligationV1, ...],
    reviewed: Mapping[str, tuple[str, ...]],
) -> dict[str, frozenset[str]]:
    categories = {
        "constraint_coverage": {"api", "negative", "data_integrity"},
        "auth_matrix_coverage": {"api", "negative", "data_integrity"},
        "journey_coverage": {"e2e", "e2e_if_enabled"},
    }[goal]
    scope = {key: frozenset(case_ids) for key, case_ids in reviewed.items()}
    for obligation in baseline:
        if obligation.required and obligation.category in categories:
            if goal == "constraint_coverage" and ".constraints." not in obligation.key:
                continue
            if goal == "auth_matrix_coverage" and not obligation.key.startswith(("auth.", "auth_matrix.")):
                continue
            scope.setdefault(obligation.key, frozenset())
    return dict(sorted(scope.items()))


__all__ = ["goal_case_map"]
