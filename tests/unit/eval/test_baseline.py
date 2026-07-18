from pathlib import Path

import pytest

from assurance_agent.eval.baseline import compare_with_baseline, read_baseline, update_baseline
from assurance_agent.eval.types import RunManifest, SuiteMetrics


def _run(root: Path, run_id: str, suite: str, metrics: dict[str, float]) -> Path:
    run = root / "eval/out/runs" / run_id
    run.mkdir(parents=True)
    manifest = RunManifest(
        run_id=run_id,
        suite=suite,
        scorer=suite,
        selected_sample_ids=["S-1"],
        total_samples=1,
        executed_samples=1,
        target_model="m",
        suite_version="7",
        repeat=3,
        regression_policy_sha256="a" * 64,
        started_at="2026-07-15T00:00:00Z",
        completed_at="2026-07-15T00:01:00Z",
    )
    (run / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    value = SuiteMetrics(run_id=run_id, suite=suite, sample_count=1, metrics=metrics)
    (run / "metrics.json").write_text(value.model_dump_json(), encoding="utf-8")
    return run


def test_compare_only_shared_metrics(tmp_path: Path) -> None:
    run = _run(tmp_path, "r1", "s1", {"shared": 0.8, "new": 0.4})
    assert compare_with_baseline(run, {"shared": 0.5, "old": 1.0}) == {"shared": pytest.approx(0.3)}


def test_read_baseline_missing_returns_empty(tmp_path: Path) -> None:
    assert read_baseline(tmp_path) == {}


def test_update_preserves_other_suites(tmp_path: Path) -> None:
    first = _run(tmp_path, "r1", "s1", {"x": 0.5})
    second = _run(tmp_path, "r2", "s2", {"y": 0.8})
    update_baseline(tmp_path, suite_name="s1", run_id=first.name, approved_by="a")
    update_baseline(tmp_path, suite_name="s2", run_id=second.name, approved_by="b")
    baseline = read_baseline(tmp_path)
    assert set(baseline) == {"s1", "s2"}
    assert baseline["s1"].run_id == "r1"
    assert baseline["s1"].suite_version == "7"
    assert baseline["s1"].repeat == 3
    assert baseline["s1"].regression_policy_sha256 == "a" * 64
