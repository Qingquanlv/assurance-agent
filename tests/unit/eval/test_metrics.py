from __future__ import annotations

from assurance_agent.eval.metrics import aggregate_scores
from assurance_agent.eval.types import SampleScore


def test_aggregate_averages_across_samples() -> None:
    scores = [
        SampleScore(sample_id="A", metrics={"api_pass_rate": 1.0, "secret_leak_count": 0}),
        SampleScore(sample_id="B", metrics={"api_pass_rate": 0.5, "secret_leak_count": 2}),
    ]
    m = aggregate_scores("run-1", "workflow-run", scores)
    assert m.sample_count == 2
    assert m.metrics["api_pass_rate"] == 0.75
    assert m.metrics["secret_leak_count"] == 1.0  # 均值 (0+2)/2
    assert m.per_sample["B"]["api_pass_rate"] == 0.5


def test_aggregate_ignores_errored_samples_in_mean() -> None:
    scores = [
        SampleScore(sample_id="A", status="ok", metrics={"x": 1.0}),
        SampleScore(sample_id="B", status="error", metrics={}, error="boom"),
    ]
    m = aggregate_scores("run-1", "s", scores)
    assert m.metrics["x"] == 1.0  # 只对成功 sample 求均值
    assert m.sample_count == 2
    assert m.error_count == 1
    assert "B" in m.per_sample
