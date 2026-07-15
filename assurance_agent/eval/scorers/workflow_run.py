from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "test_executable_rate": shared.score_test_executable_rate_e3(raw),
            "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
            "forbidden_write_executed_count":
                shared.score_forbidden_write_executed_count(attempt_dir),
            "execution_pass_rate": shared.score_execution_pass_rate(raw),
            "api_pass_rate": shared.score_layer_pass_rate(raw, "api"),
            "e2e_pass_rate": shared.score_layer_pass_rate(raw, "e2e"),
            "fuzz_pass_rate": shared.score_layer_pass_rate(raw, "fuzz"),
            "performance_pass_rate": shared.score_layer_pass_rate(raw, "performance"),
        },
    )
