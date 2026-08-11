"""M2 Task 5: compute-baseline-drift vs project append-only baseline history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.metrics import PR_METRICS_REL
from assurance_agent.artifacts.models.pr_metric_evidence import BaselineDriftEvidence
from assurance_agent.verification.baseline_history import (
    DEFAULT_BASELINE_WINDOW,
    DEFAULT_MIN_BASELINE_SAMPLES,
    PerformanceBaselineObservation,
    append_baseline_observation,
    recent_baseline_observations,
    scenario_identity,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.baseline_drift import (
    BASELINE_DRIFT_BATCH_ID,
    BASELINE_DRIFT_EVIDENCE_REL,
    DEFAULT_DRIFT_BAND,
    compute_baseline_drift,
    compute_baseline_drift_operation,
)
from assurance_agent.workflow.metrics.nightly import (
    BASELINE_DRIFT_EVIDENCE_REL as NIGHTLY_BASELINE_REL,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-BASE-001"
SOURCE_BATCH = "batch-src"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write_aa_config(root)
    change = root / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    return root


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t-baseline",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-baseline",
        node_id="compute-baseline-drift",
        graph_id="metrics-nightly-workflow",
        target="operation:compute-baseline-drift",
        input={"with": {}},
    )


def _write_perf(
    project_root: Path,
    *,
    p95_ms: float = 12.0,
    error_rate: float = 0.0,
    capability: str = "login",
    endpoint: str = "POST /api/login",
    batch_id: str = SOURCE_BATCH,
) -> Path:
    change = project_root / "qa" / "changes" / CHANGE_ID
    batch = change / "execution" / "runs" / batch_id
    batch.mkdir(parents=True, exist_ok=True)
    manifest = change / "execution" / "execution-manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": batch_id,
                "executed_at": "2026-08-05T10:00:00+00:00",
                "selected_targets": {
                    "api": False,
                    "e2e": False,
                    "fuzz": False,
                    "performance": True,
                },
                "result_files": {"performance": "performance-result.json"},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    path = batch / "performance-result.json"
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "kind": "performance",
        "available": True,
        "status": "PASS",
        "scenarios": [
            {
                "capability": capability,
                "endpoint": endpoint,
                "measured_p95_ms": p95_ms,
                "threshold_p95_ms": 2000.0,
                "measured_error_rate": error_rate,
                "threshold_error_rate_max": 0.01,
                "verdict": "PASS",
            }
        ],
        "command": "locust",
        "source": {"raw_log": "raw/perf.log"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_history(
    project_root: Path,
    *,
    count: int,
    p95_ms: float = 10.0,
    error_rate: float = 0.0,
    capability: str = "login",
    endpoint: str = "POST /api/login",
) -> None:
    for i in range(count):
        append_baseline_observation(
            project_root,
            PerformanceBaselineObservation(
                capability=capability,
                endpoint=endpoint,
                change_id=f"CH-HIST-{i}",
                batch_id=f"hist-{i}",
                p95_ms=p95_ms,
                error_rate=error_rate,
                source_digest=f"{i:064x}",
            ),
        )


def test_evidence_path_matches_nightly_contract_surface() -> None:
    assert BASELINE_DRIFT_EVIDENCE_REL == NIGHTLY_BASELINE_REL
    assert BASELINE_DRIFT_EVIDENCE_REL == "execution/runs/nightly/baseline-drift.json"
    assert BASELINE_DRIFT_BATCH_ID == "nightly"
    assert DEFAULT_DRIFT_BAND > 0
    assert DEFAULT_MIN_BASELINE_SAMPLES <= DEFAULT_BASELINE_WINDOW


def test_insufficient_samples_are_not_evaluated(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_perf(root, p95_ms=12.0)
    _seed_history(root, count=DEFAULT_MIN_BASELINE_SAMPLES - 1, p95_ms=10.0)

    evidence = compute_baseline_drift(
        project_root=root, change_id=CHANGE_ID, change_dir=_workspace(root).change_dir
    )
    assert evidence.status == "not_evaluated"
    assert evidence.value is None
    assert any(b.code == "sample_insufficient" and b.metric == "baseline_drift" for b in evidence.shortboards)
    # Absolute-threshold inconclusive is not invented here as a hard gate/gap.
    assert not any(g.code in {"collection_failed", "artifact_corrupt"} for g in evidence.collection_gaps)


def test_out_of_band_drift_is_shortboard_only(tmp_path: Path) -> None:
    root = _project(tmp_path)
    baseline_p95 = 10.0
    # 50% regression → above default 20% band.
    _write_perf(root, p95_ms=baseline_p95 * 1.5)
    _seed_history(root, count=DEFAULT_MIN_BASELINE_SAMPLES, p95_ms=baseline_p95)

    evidence = compute_baseline_drift(
        project_root=root, change_id=CHANGE_ID, change_dir=_workspace(root).change_dir
    )
    assert evidence.status == "evaluated"
    assert evidence.value is not None
    assert evidence.value == pytest.approx(0.5)
    assert any(
        b.code == "baseline_drift_out_of_band" and b.metric == "baseline_drift" for b in evidence.shortboards
    )
    # Never a collection gap / hard fail for out-of-band drift.
    assert evidence.collection_gaps == ()


def test_within_band_evaluates_without_shortboard(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_perf(root, p95_ms=11.0)  # 10% vs baseline 10
    _seed_history(root, count=DEFAULT_MIN_BASELINE_SAMPLES, p95_ms=10.0)

    evidence = compute_baseline_drift(
        project_root=root, change_id=CHANGE_ID, change_dir=_workspace(root).change_dir
    )
    assert evidence.status == "evaluated"
    assert evidence.value == pytest.approx(0.1)
    assert not any(b.code == "baseline_drift_out_of_band" for b in evidence.shortboards)


def test_operation_appends_history_and_never_writes_pr_metrics(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workspace = _workspace(root)
    _write_perf(root, p95_ms=12.0)
    _seed_history(root, count=DEFAULT_MIN_BASELINE_SAMPLES, p95_ms=10.0)

    pr = workspace.change_dir / PR_METRICS_REL
    pr.parent.mkdir(parents=True, exist_ok=True)
    planted = b'{"planted":true}\n'
    pr.write_bytes(planted)

    before = recent_baseline_observations(
        root,
        identity=scenario_identity("login", "POST /api/login"),
        limit=100,
    )
    result = compute_baseline_drift_operation(_task(), workspace, _context(root))
    assert result.status == "succeeded"
    assert pr.read_bytes() == planted

    path = workspace.change_dir / BASELINE_DRIFT_EVIDENCE_REL
    assert path.is_file()
    payload = BaselineDriftEvidence.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert payload.change_id == CHANGE_ID
    assert payload.batch_id == "nightly"
    assert "stub pending M2 Task 5" not in path.read_text(encoding="utf-8")

    after = recent_baseline_observations(
        root,
        identity=scenario_identity("login", "POST /api/login"),
        limit=100,
    )
    assert len(after) == len(before) + 1
    assert after[0].batch_id == SOURCE_BATCH
    assert after[0].p95_ms == 12.0
    assert len(after[0].source_digest) == 64


def test_rerun_keeps_stable_recent_n_mean_after_self_indexed(tmp_path: Path) -> None:
    """Excluding current batch must still leave up to ``window`` prior samples.

    Old window-shrink bug: fetch ``window`` then drop self → mean of ``window-1``
    on rerun, so the same payload changes solely because self is now indexed.
    """
    root = _project(tmp_path)
    change_dir = _workspace(root).change_dir
    # Distinct priors so shrinking the window changes the mean.
    for i in range(DEFAULT_BASELINE_WINDOW):
        append_baseline_observation(
            root,
            PerformanceBaselineObservation(
                capability="login",
                endpoint="POST /api/login",
                change_id=f"CH-HIST-{i}",
                batch_id=f"hist-{i}",
                p95_ms=float(10 + i),
                error_rate=0.0,
                source_digest=f"{i:064x}",
            ),
        )
    expected_baseline = sum(float(10 + i) for i in range(DEFAULT_BASELINE_WINDOW)) / DEFAULT_BASELINE_WINDOW
    current_p95 = expected_baseline * 1.1
    _write_perf(root, p95_ms=current_p95)

    first = compute_baseline_drift(project_root=root, change_id=CHANGE_ID, change_dir=change_dir)
    assert first.status == "evaluated"
    assert first.scenarios[0].sample_count == DEFAULT_BASELINE_WINDOW
    assert first.scenarios[0].baseline_p95_ms == pytest.approx(expected_baseline)
    assert first.value == pytest.approx(0.1)

    second = compute_baseline_drift(project_root=root, change_id=CHANGE_ID, change_dir=change_dir)
    assert second.status == "evaluated"
    assert second.scenarios[0].sample_count == DEFAULT_BASELINE_WINDOW
    assert second.scenarios[0].baseline_p95_ms == pytest.approx(expected_baseline)
    assert second.value == pytest.approx(first.value)


def test_mixed_cold_warm_scenarios_are_whole_metric_not_evaluated(tmp_path: Path) -> None:
    """Any cold verified scenario forces not_evaluated; do not evaluate a warm subset."""
    root = _project(tmp_path)
    change_dir = _workspace(root).change_dir
    _seed_history(
        root,
        count=DEFAULT_MIN_BASELINE_SAMPLES,
        p95_ms=10.0,
        capability="login",
        endpoint="POST /api/login",
    )
    # Cold scenario: no prior history.
    change = change_dir
    batch = change / "execution" / "runs" / SOURCE_BATCH
    batch.mkdir(parents=True, exist_ok=True)
    manifest = change / "execution" / "execution-manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": SOURCE_BATCH,
                "executed_at": "2026-08-05T10:00:00+00:00",
                "selected_targets": {
                    "api": False,
                    "e2e": False,
                    "fuzz": False,
                    "performance": True,
                },
                "result_files": {"performance": "performance-result.json"},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    (batch / "performance-result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": SOURCE_BATCH,
                "kind": "performance",
                "available": True,
                "status": "PASS",
                "scenarios": [
                    {
                        "capability": "login",
                        "endpoint": "POST /api/login",
                        "measured_p95_ms": 12.0,
                        "threshold_p95_ms": 2000.0,
                        "measured_error_rate": 0.0,
                        "threshold_error_rate_max": 0.01,
                        "verdict": "PASS",
                    },
                    {
                        "capability": "other",
                        "endpoint": "GET /other",
                        "measured_p95_ms": 20.0,
                        "threshold_p95_ms": 2000.0,
                        "measured_error_rate": 0.0,
                        "threshold_error_rate_max": 0.01,
                        "verdict": "PASS",
                    },
                ],
                "command": "locust",
                "source": {"raw_log": "raw/perf.log"},
            }
        ),
        encoding="utf-8",
    )

    evidence = compute_baseline_drift(project_root=root, change_id=CHANGE_ID, change_dir=change_dir)
    assert evidence.status == "not_evaluated"
    assert evidence.value is None
    assert evidence.scenarios == ()
    assert any(b.code == "sample_insufficient" and b.metric == "baseline_drift" for b in evidence.shortboards)

    # Both verified scenarios are still indexed even when the metric is not evaluated.
    warm = recent_baseline_observations(
        root, identity=scenario_identity("login", "POST /api/login"), limit=100
    )
    cold = recent_baseline_observations(root, identity=scenario_identity("other", "GET /other"), limit=100)
    assert any(row.change_id == CHANGE_ID and row.batch_id == SOURCE_BATCH for row in warm)
    assert any(row.change_id == CHANGE_ID and row.batch_id == SOURCE_BATCH for row in cold)
