"""Eval path helpers — dual root: engine data vs SUT out.

Data (suites / datasets / baselines / suts.yaml) lives under the engine repo
``eval/``. Run artifacts (``out/runs``, ``out/reports``) live under the SUT
``eval/out/`` so ``read_eval_trend(sut_root)`` sees them.
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe


def eval_root(engine_root: Path) -> Path:
    """Engine-side eval data root (suites, datasets, baselines)."""
    return engine_root / "eval"


def eval_data_root(engine_root: Path) -> Path:
    return eval_root(engine_root)


def eval_out_root(sut_root: Path) -> Path:
    """SUT-side eval output root."""
    return sut_root / "eval" / "out"


def datasets_dir(engine_root: Path, suite: str) -> Path:
    assert_path_segment_safe(suite, label="eval suite")
    return eval_root(engine_root) / "datasets" / suite


def runs_dir(sut_root: Path) -> Path:
    return eval_out_root(sut_root) / "runs"


def run_dir(sut_root: Path, run_id: str) -> Path:
    assert_path_segment_safe(run_id, label="eval run id")
    return runs_dir(sut_root) / run_id


def samples_dir(sut_root: Path, run_id: str) -> Path:
    return run_dir(sut_root, run_id) / "samples"


def attempt_dir(sut_root: Path, run_id: str, sample_id: str, attempt: int) -> Path:
    assert_path_segment_safe(sample_id, label="eval sample id")
    return samples_dir(sut_root, run_id) / sample_id / f"attempt-{attempt}"


def reports_dir(sut_root: Path) -> Path:
    return eval_out_root(sut_root) / "reports"
