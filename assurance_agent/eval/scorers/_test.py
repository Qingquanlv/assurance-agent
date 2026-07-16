from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    has_output = raw.is_dir() and any(raw.iterdir())
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "pass_rate": 1.0 if has_output else 0.0,
        },
    )
