from __future__ import annotations

import pytest

from tests.product.execution_loop import (
    assert_each_coverage_repair_is_preceded_by_one_advance,
    bind_installed_sources,
    completed_node_ids,
    drive_coverage_loop,
    execute_tail_coverage_state,
)

pytestmark = pytest.mark.usefixtures("installed_sources", "product_runner")


@pytest.fixture(autouse=True)
def _bind_sources(installed_sources) -> None:
    bind_installed_sources(installed_sources)


def test_low_coverage_reenters_generation_until_policy_passes(product_runner):
    trace = product_runner(coverage_sequence=(0.40, 0.72, 0.91), threshold=0.90).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 2
    assert trace.report.coverage == 0.91
    assert trace.status == "succeeded"
    assert trace.report.exists


def test_coverage_repair_exhaustion_stops_with_a_report(product_runner):
    trace = product_runner(
        coverage_sequence=(0.40, 0.41, 0.42),
        threshold=0.90,
        coverage_rounds=1,
    ).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 1
    assert trace.status in {"stopped", "succeeded"}
    assert trace.report.exists
    assert "quality.report" in trace.logical_steps


def test_measured_above_threshold_is_satisfied_without_repair() -> None:
    trace = drive_coverage_loop(
        coverage_states=("satisfied",),
        measured_sequence=(0.91,),
        coverage_rounds=2,
    )
    assert "quality.assess" in trace.public_exports
    assert "healing.repair-coverage" not in trace.public_exports
    assert trace.advance_outputs == ()
    assert trace.terminal != "not-achieved"
    assert "quality.report" in trace.public_exports


def test_measured_equal_threshold_is_satisfied_without_repair() -> None:
    trace = drive_coverage_loop(
        coverage_states=("satisfied",),
        measured_sequence=(0.90,),
        threshold=0.90,
        coverage_rounds=2,
    )
    assert "healing.repair-coverage" not in trace.public_exports
    assert trace.advance_outputs == ()
    assert "quality.report" in trace.public_exports


def test_below_threshold_with_budget_repairs_then_reassesses() -> None:
    trace = drive_coverage_loop(
        coverage_states=("repair_required", "satisfied"),
        repair_statuses=("repaired",),
        measured_sequence=(0.40, 0.91),
        coverage_rounds=2,
    )
    assert "healing.repair-coverage" in trace.public_exports
    assert_each_coverage_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert trace.advance_outputs == ({"kind": "coverage", "rounds_used": 1, "rounds_budget": 2},)
    assert "quality.report" in trace.public_exports
    assert trace.terminal != "not-achieved"


def test_zero_budget_is_exhausted_without_advance_or_repair() -> None:
    trace = drive_coverage_loop(
        coverage_states=("exhausted",),
        measured_sequence=(0.40,),
        coverage_rounds=0,
    )
    assert "healing.repair-coverage" not in trace.public_exports
    assert trace.advance_outputs == ()
    assert not any(item.endswith("coverage-repair.finalize") for item in trace.task_capabilities)
    assert not any(item.endswith("repair-round.advance") for item in trace.task_capabilities)
    assert trace.terminal != "achieved"


def test_exhausted_budget_does_not_dispatch_another_repair() -> None:
    trace = drive_coverage_loop(
        coverage_states=("repair_required", "exhausted"),
        repair_statuses=("repaired",),
        measured_sequence=(0.40, 0.41),
        coverage_rounds=1,
    )
    assert_each_coverage_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert tuple(item["rounds_used"] for item in trace.advance_outputs) == (1,)
    assert all(item["kind"] == "coverage" for item in trace.advance_outputs)
    assert all(item["rounds_used"] <= item["rounds_budget"] for item in trace.advance_outputs)
    assert trace.terminal != "achieved"
    assert "quality.report" in trace.public_exports


def test_needs_human_interrupts_without_repair() -> None:
    trace = drive_coverage_loop(coverage_states=("needs_human",), measured_sequence=(0.40,))
    assert "healing.repair-coverage" not in trace.public_exports
    assert trace.advance_outputs == ()
    assert trace.status == "interrupted"
    assert trace.terminal != "achieved"


def test_inconclusive_reports_without_reaching_achieved() -> None:
    trace = drive_coverage_loop(coverage_states=("inconclusive",), measured_sequence=(0.40,))
    assert "healing.repair-coverage" not in trace.public_exports
    assert trace.advance_outputs == ()
    assert "quality.report" in trace.public_exports
    assert trace.terminal != "achieved"


@pytest.mark.parametrize("status", ["not_eligible", "exhausted", "failed"])
def test_non_repaired_coverage_outcome_is_explicit_non_achieved(status: str) -> None:
    trace = drive_coverage_loop(
        coverage_states=("repair_required",),
        repair_statuses=(status,),
        measured_sequence=(0.40,),
        coverage_rounds=2,
    )
    assert "healing.repair-coverage" in trace.public_exports
    assert_each_coverage_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert tuple(item["rounds_used"] for item in trace.advance_outputs) == (1,)
    assert "quality.report" in trace.public_exports
    assert trace.terminal != "achieved"


def test_needs_review_interrupts_after_authenticated_advance() -> None:
    trace = drive_coverage_loop(
        coverage_states=("repair_required",),
        repair_statuses=("needs_review",),
        measured_sequence=(0.40,),
        coverage_rounds=2,
    )
    assert "healing.repair-coverage" in trace.public_exports
    assert_each_coverage_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert trace.advance_outputs == ({"kind": "coverage", "rounds_used": 1, "rounds_budget": 2},)
    assert trace.status == "interrupted"
    assert trace.terminal != "achieved"


def test_repaired_rounds_are_monotonic_and_never_exceed_budget() -> None:
    trace = drive_coverage_loop(
        coverage_states=("repair_required", "repair_required", "satisfied"),
        repair_statuses=("repaired", "repaired"),
        measured_sequence=(0.40, 0.72, 0.91),
        coverage_rounds=2,
    )
    used = tuple(item["rounds_used"] for item in trace.advance_outputs)
    assert used == (1, 2)
    assert used == tuple(sorted(used))
    assert all(item["kind"] == "coverage" for item in trace.advance_outputs)
    assert all(item["rounds_used"] <= item["rounds_budget"] == 2 for item in trace.advance_outputs)
    assert_each_coverage_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert "quality.report" in trace.public_exports


@pytest.mark.parametrize(
    ("coverage_states", "repair_statuses", "coverage_rounds", "terminal", "assess_state"),
    [
        (("satisfied",), (), 2, "achieved", "satisfied"),
        (("repair_required", "satisfied"), ("repaired",), 2, "achieved", "satisfied"),
        (("exhausted",), (), 0, "not-achieved", "exhausted"),
        (("repair_required", "exhausted"), ("repaired",), 1, "not-achieved", "exhausted"),
        (("inconclusive",), (), 1, "not-achieved", "inconclusive"),
        (("repair_required",), ("failed",), 2, "not-achieved", "repair_required"),
        (("repair_required",), ("not_eligible",), 2, "not-achieved", "repair_required"),
        (("repair_required",), ("exhausted",), 2, "not-achieved", "repair_required"),
    ],
)
def test_full_retro_and_achieved_require_fresh_assess_satisfied(
    coverage_states: tuple[str, ...],
    repair_statuses: tuple[str, ...],
    coverage_rounds: int,
    terminal: str,
    assess_state: str,
) -> None:
    trace = drive_coverage_loop(
        coverage_states=coverage_states,
        repair_statuses=repair_statuses,
        coverage_rounds=coverage_rounds,
        measured_sequence=(0.40, 0.41, 0.91)[: max(len(coverage_states), 1)],
        entrypoint="full",
    )
    nodes = completed_node_ids(trace.projection)
    assert execute_tail_coverage_state(trace.projection) == assess_state
    assert trace.terminal == terminal
    if terminal == "achieved":
        assert "retro" in nodes
        assert "achieved" in nodes
        return
    assert "retro" not in nodes
    assert "achieved" not in nodes


def test_full_needs_human_does_not_reach_achieved() -> None:
    trace = drive_coverage_loop(
        coverage_states=("needs_human",),
        measured_sequence=(0.40,),
        entrypoint="full",
    )
    nodes = completed_node_ids(trace.projection)
    assert trace.status == "interrupted"
    assert trace.terminal != "achieved"
    assert "retro" not in nodes
    assert "achieved" not in nodes
    assert execute_tail_coverage_state(trace.projection) is None
