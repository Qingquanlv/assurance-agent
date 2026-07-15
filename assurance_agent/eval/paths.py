from __future__ import annotations

from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe


def eval_root(project_root: Path) -> Path:
    return project_root / "eval"


def datasets_dir(project_root: Path, suite: str) -> Path:
    assert_path_segment_safe(suite, label="eval suite")
    return eval_root(project_root) / "datasets" / suite


def runs_dir(project_root: Path) -> Path:
    return eval_root(project_root) / "out" / "runs"


def run_dir(project_root: Path, run_id: str) -> Path:
    assert_path_segment_safe(run_id, label="eval run id")
    return runs_dir(project_root) / run_id


def samples_dir(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / "samples"


def attempt_dir(project_root: Path, run_id: str, sample_id: str, attempt: int) -> Path:
    assert_path_segment_safe(sample_id, label="eval sample id")
    return samples_dir(project_root, run_id) / sample_id / f"attempt-{attempt}"


def reports_dir(project_root: Path) -> Path:
    return eval_root(project_root) / "out" / "reports"
