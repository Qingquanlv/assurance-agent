from __future__ import annotations

import pytest

from graph_engine.runtime.engine import EngineError

pytestmark = pytest.mark.usefixtures("product_runner")


def test_business_stop_is_resumable_only_at_declared_interrupt(product_runner):
    stopped = product_runner(review_decision="needs-human").run_to_terminal()
    assert stopped.status == "interrupted"
    resumed = stopped.resume({"decision": "approve"})
    assert resumed.status == "completed"


def test_invalid_resume_input_fails(product_runner):
    stopped = product_runner(review_decision="needs-human").run_to_terminal()
    assert stopped.status == "interrupted"
    with pytest.raises(EngineError):
        stopped.resume({"decision": "not-allowed"})
    with pytest.raises(EngineError):
        stopped.resume({"decision": "approve", "extra": "field"})
    still_pending = product_runner(review_decision="needs-human").run_to_terminal()
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
