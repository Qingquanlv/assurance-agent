from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    manifest = shared.read_yaml(raw / "execution" / "execution-manifest.json")
    completed = 1.0 if manifest else 0.0
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "full_run_completed_rate": completed,
            "end_to_end_pass_rate": shared.score_execution_pass_rate(raw),
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
        },
    )
