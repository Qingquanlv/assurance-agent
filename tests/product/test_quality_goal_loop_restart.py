"""Resume after a flow-node crash reuses the checkpoint and does not redispath."""

from __future__ import annotations

from typing import cast

import pytest

from tests.product.flow_checkpoint import resume_flow_checkpoint, run_flow_checkpoint


@pytest.mark.parametrize(
    "cut",
    (
        "after_coverage_advance",
        "after_application_commit",
        "after_inspect_commit",
    ),
)
def test_resume_preserves_business_identity_and_committed_writes(tmp_path, cut) -> None:
    run_flow_checkpoint(tmp_path, cut=cut)
    resumed = resume_flow_checkpoint(tmp_path)

    assert resumed.state["status"] == "completed"
    assert resumed.coverage_epochs == (0, 1)
    assert resumed.node_visits.count("prepare") == 1
    assert resumed.dispatch_count("healing.apply-test-repair") == 1
    assert resumed.dispatch_count("execution.run") == 1
    assert resumed.dispatch_count("quality.inspect") == 3
    assert resumed.dispatch_count("quality.report") == 1
    assert resumed.node_visits.count("retro") == 0


def test_new_epoch_uses_distinct_case_execution_and_inspect_keys_for_same_case_bytes(tmp_path) -> None:
    run_flow_checkpoint(tmp_path, cut="after_coverage_advance")
    resumed = resume_flow_checkpoint(tmp_path)

    for semantic_id in ("intake.case-design", "execution.execute", "quality.inspect"):
        keys = resumed.attempt_keys[semantic_id]
        assert len(keys) == len(set(keys))
    assert len(resumed.attempt_keys["intake.case-design"]) == 2
    assert resumed.coverage_epochs == (0, 1)


def test_resume_runs_in_a_fresh_process_and_reuses_the_sqlite_checkpoint(tmp_path) -> None:
    interrupted = run_flow_checkpoint(tmp_path, cut="after_coverage_advance")
    control = cast(dict[str, object], interrupted.state["flow_control"])
    loops = cast(dict[str, int], control["loops"])
    assert loops["coverage"] == 1
    assert interrupted.node_visits.count("prepare") == 1

    resumed = resume_flow_checkpoint(tmp_path)

    assert resumed.state["status"] == "completed"
    assert resumed.node_visits.count("prepare") == 1
    assert any(path.name == "goal-loop-checkpoints.sqlite" for path in resumed.artifact_paths)
