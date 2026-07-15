from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore

_LAYER = {
    "workflow-api-codegen": ("tests/api", "codegen/api-codegen-summary.md"),
    "workflow-e2e-codegen": ("tests/e2e", "codegen/e2e-codegen-summary.md"),
    "workflow-fuzz-codegen": ("tests/fuzz", "codegen/fuzz-codegen-summary.md"),
    "workflow-performance-codegen": ("tests/perf", "codegen/performance-codegen-summary.md"),
}


def make_scorer(suite: str):
    tests_subdir, summary_rel = _LAYER[suite]

    def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
        raw = shared.raw_output_dir(attempt_dir)
        return SampleScore(
            sample_id=sample.id,
            status="ok",
            metrics={
                "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
                "schema_valid_rate": shared.score_py_syntax_valid_rate(raw / tests_subdir),
                "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
                "forbidden_write_executed_count": shared.score_forbidden_write_executed_count(attempt_dir),
                "codegen_summary_present_rate": shared.score_present_rate(raw / summary_rel),
            },
        )

    return score
