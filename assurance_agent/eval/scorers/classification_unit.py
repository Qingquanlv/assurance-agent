from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore
from assurance_agent.workflow.report.failure_classifier import classify_failure


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    inp = sample.input
    message = str(inp.get("message", ""))
    log_excerpt = str(inp.get("log_excerpt", ""))
    target = inp.get("target", "api")
    if target not in ("api", "e2e", "fuzz"):
        target = "api"

    classified = classify_failure(
        message=message,
        log_excerpt=log_excerpt,
        target=target,  # type: ignore[arg-type]
        has_trace=False,
        has_screenshot=False,
    )

    expected_category = sample.expected.get("category")
    if expected_category:
        category_match_rate = 1.0 if classified.category == expected_category else 0.0
    else:
        category_match_rate = 0.0

    unknown_rate = 1.0 if classified.category == "unknown" else 0.0

    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "category_match_rate": category_match_rate,
            "unknown_rate": unknown_rate,
        },
    )
