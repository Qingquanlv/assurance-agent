from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.paths import (
    attempt_dir,
    datasets_dir,
    eval_data_root,
    eval_out_root,
    reports_dir,
    run_dir,
    runs_dir,
)


def test_dual_root_paths(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    sut = tmp_path / "sut"
    assert eval_data_root(engine) == engine / "eval"
    assert eval_out_root(sut) == sut / "eval" / "out"
    assert datasets_dir(engine, "workflow-run") == engine / "eval" / "datasets" / "workflow-run"
    assert runs_dir(sut) == sut / "eval" / "out" / "runs"
    assert run_dir(sut, "eval-1") == sut / "eval" / "out" / "runs" / "eval-1"
    assert attempt_dir(sut, "eval-1", "WR-001", 0) == (
        sut / "eval" / "out" / "runs" / "eval-1" / "samples" / "WR-001" / "attempt-0"
    )
    assert reports_dir(sut) == sut / "eval" / "out" / "reports"
