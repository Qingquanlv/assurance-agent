"""Merge intake quality obligations with reviewed Case evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from assurance_intake.contracts.quality_goals import CoverageGoal, MrcCategory, MrcLayer, PreparedObligationV1


def obligation_goal(*, key: str, category: MrcCategory) -> CoverageGoal | None:
    if category in {"e2e", "e2e_if_enabled"}:
        return "journey_coverage"
    if ".constraints." in key:
        return "constraint_coverage"
    if key.startswith(("auth.", "auth_matrix.")):
        return "auth_matrix_coverage"
    return None


def has_layer_evidence(layer: MrcLayer, case_ids: Iterable[str], case_layers: Mapping[str, str]) -> bool:
    required = {"api", "e2e"} if layer == "both" else {layer}
    return required <= {case_layers.get(case_id) for case_id in case_ids}


def goal_case_map(
    goal: CoverageGoal,
    *,
    baseline: tuple[PreparedObligationV1, ...],
    reviewed: Mapping[str, tuple[str, ...]],
) -> dict[str, frozenset[str]]:
    scope = {key: frozenset(case_ids) for key, case_ids in reviewed.items()}
    for obligation in baseline:
        if obligation.required and obligation_goal(key=obligation.key, category=obligation.category) == goal:
            scope.setdefault(obligation.key, frozenset())
    return dict(sorted(scope.items()))


__all__ = ["goal_case_map", "has_layer_evidence", "obligation_goal"]
