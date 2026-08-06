"""M2 nightly pipeline: aggregate carrier; never reopen PR metrics."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.models.metrics import MetricsDocument, NIGHTLY_METRICS_REL, PR_METRICS_REL
from assurance_agent.evidence.metrics import DEFAULT_NIGHTLY_KEYS
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.adversarial_yield import collect_adversarial_yield_operation
from assurance_agent.workflow.metrics.nightly import (
    aggregate_nightly_metrics_operation,
    compute_assertion_strength_operation,
    compute_baseline_drift_operation,
    evaluate_retrospective_shortboards_operation,
    load_latest_pr_metrics_operation,
    run_mutation_sample_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-NIGHTLY-001"
PR_BYTES = b'{"planted":"pr-metrics-must-not-change"}\n'

_PIPELINE = (
    ("operation:load-latest-pr-metrics", load_latest_pr_metrics_operation),
    ("operation:run-mutation-sample", run_mutation_sample_operation),
    ("operation:compute-assertion-strength", compute_assertion_strength_operation),
    ("operation:compute-baseline-drift", compute_baseline_drift_operation),
    ("operation:collect-adversarial-yield", collect_adversarial_yield_operation),
    ("operation:aggregate-nightly-metrics", aggregate_nightly_metrics_operation),
    ("operation:evaluate-retrospective-shortboards", evaluate_retrospective_shortboards_operation),
)


def _workspace(project_root: Path) -> TaskWorkspace:
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    return TaskWorkspace(
        task_id="t-nightly",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )


def _task(target: str) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-nightly",
        node_id=target.removeprefix("operation:"),
        graph_id="metrics-nightly-workflow",
        target=target,
        input={"with": {}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def test_nightly_pipeline_writes_nightly_and_leaves_pr_metrics_untouched(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    pr_path = workspace.change_dir / PR_METRICS_REL
    pr_path.parent.mkdir(parents=True, exist_ok=True)
    pr_path.write_bytes(PR_BYTES)

    for target, op in _PIPELINE:
        result = op(_task(target), workspace, context)
        assert result.status == "succeeded", target

    assert pr_path.read_bytes() == PR_BYTES

    nightly_path = workspace.change_dir / NIGHTLY_METRICS_REL
    assert nightly_path.is_file()
    doc = MetricsDocument.model_validate(json.loads(nightly_path.read_text(encoding="utf-8")))
    assert doc.change_id == CHANGE_ID
    assert doc.cadence == "nightly"
    assert set(DEFAULT_NIGHTLY_KEYS) <= set(doc.metrics)
    # Empty fixture: mutation/assertion stay pending; baseline may collection_fail
    # when no execution manifest exists (Task 5 collector truth).
    assert doc.metrics["mutation_score"].status == "not_evaluated"
    assert doc.metrics["assertion_strength"].status == "not_evaluated"
    assert doc.metrics["baseline_drift"].status in {"not_evaluated", "collection_failed"}
    # B3 must stay not_evaluated without an invented collection gap.
    assert doc.metrics["adversarial_yield"].status == "not_evaluated"
    assert not any(
        gap.code in {"collection_failed", "artifact_corrupt"} and gap.metric == "adversarial_yield"
        for gap in doc.collection_gaps
    )
    pending = {board.metric for board in doc.shortboards if board.code == "pending_nightly"}
    assert {"mutation_score", "assertion_strength", "adversarial_yield"} <= pending
    assert "confidence" not in json.loads(nightly_path.read_text(encoding="utf-8"))
