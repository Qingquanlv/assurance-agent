"""Deterministic admission guard for repairs to verified generated tests."""

from __future__ import annotations

from graph_engine.canonical import canonical_json_bytes

from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1


def assert_same_obligations(before: CaseExecutionPlanV1, after: CaseExecutionPlanV1) -> None:
    """Reject any repair that changes the frozen business verification contract."""

    if canonical_json_bytes(before.obligation_projection()) != canonical_json_bytes(
        after.obligation_projection()
    ):
        raise ValueError("verification obligations changed")


__all__ = ["assert_same_obligations"]
