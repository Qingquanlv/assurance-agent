from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.runtime.engine import EngineError

from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("product_runner")

_FORBIDDEN_TREE_NAMES = frozenset({"workspace", "trees", "attempts", "HEAD.json"})


def _intake_entry_graph(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    workflow = resolve_assurance_composition(request_for("opencode", installed_sources)).workflow
    graph = workflow.graphs.get("assurance.intake.workflow.graph.entry")
    if graph is None:
        graph = workflow.graphs["full"]
    return graph


def test_business_stop_is_resumable_only_at_declared_interrupt(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    resumed = stopped.resume({"decision": "approve"})
    if resumed.status == "interrupted":
        resumed = resumed.resume({"decision": "approve"})
    assert resumed.status == "completed"


def test_human_reject_terminates_rejected_not_completed(installed_sources):
    graph = _intake_entry_graph(installed_sources)
    edges = {(edge.from_, edge.to, edge.condition) for edge in graph.edges}
    reject_edges = {item for item in edges if item[0] == "human-review" and item[1] == "rejected"}
    assert reject_edges
    assert all(item[2] is not None and "reject" in item[2] for item in reject_edges)
    assert not any(item[0] == "human-review" and item[1] == "done" and item[2] is None for item in edges)


def test_request_rework_is_a_distinct_resume_action(installed_sources):
    graph = _intake_entry_graph(installed_sources)
    human = graph.nodes["human-review"]
    assert "request_rework" in human.definition.actions
    assert "reject" in human.definition.actions
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("human-review", "rejected") in edges
    assert any(source == "human-review" and "review-round-advance" in target for source, target in edges)


def test_invalid_resume_input_fails(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    with pytest.raises(EngineError):
        stopped.resume({"decision": "not-allowed"})
    with pytest.raises(EngineError):
        stopped.resume({"decision": "approve", "extra": "field"})
    still_pending = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert still_pending.status == "interrupted"


def test_healing_disallowed_is_business_stop_not_completion(product_runner):
    stopped = product_runner(
        execution_sequence=("failed",),
        healing_decision="disallowed",
    ).run_to_terminal()
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "healing_disallowed"
    assert stopped.status != "completed"
    assert stopped.status != "interrupted"


def test_nested_stop_does_not_become_normal_completion(product_runner):
    stopped = product_runner(
        execution_sequence=("failed",),
        healing_decision="disallowed",
    ).run_to_terminal()
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "healing_disallowed"
    assert stopped.has_nested_stop


def test_reported_success_is_distinct_from_stop_and_interrupt(product_runner):
    completed = product_runner().run_to_terminal()
    assert completed.status == "completed"
    assert completed.stop_reason is None


def test_infrastructure_failure_is_stop_after_report(product_runner):
    stopped = product_runner(execution_sequence=("infrastructure_failure",)).run_to_terminal()
    assert stopped.status == "stopped"
    assert stopped.status != "interrupted"
    assert "quality.report" in stopped.logical_steps


def test_interrupt_runtime_lives_under_the_change_without_tree_store(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    runtime = Path(stopped._engine._root)
    assert runtime.name == ".runtime"
    assert runtime.parent.parent.name == "changes"
    assert runtime.parent.parent.parent.name == "qa"
    names = {path.name for path in runtime.parent.rglob("*")}
    assert names.isdisjoint(_FORBIDDEN_TREE_NAMES)
