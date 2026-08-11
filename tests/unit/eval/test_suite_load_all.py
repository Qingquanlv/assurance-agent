from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.eval.dataset_loader import load_dataset
from assurance_agent.eval.gate import compute_gate_result
from assurance_agent.eval.paths import datasets_dir
from assurance_agent.eval.plan import load_suite
from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.types import (
    CODEGEN_HARD_METRICS,
    CODEGEN_SUITE_PENDING_TIERS,
    RunManifest,
    SuiteMetrics,
)

SUITE_NAMES = [
    "eval-smoke",
    "case-generation",
    "classification-unit",
    "safety-lite",
    "workflow-api-codegen",
    "workflow-case",
    "workflow-e2e-codegen",
    "workflow-full",
    "workflow-fuzz-codegen",
    "workflow-performance-codegen",
    "workflow-run",
]


REPO_ROOT = Path(__file__).resolve().parents[3]


def _dataset_path(suite_name: str) -> Path:
    return datasets_dir(REPO_ROOT, suite_name)


@pytest.mark.parametrize("suite_name", SUITE_NAMES)
def test_suite_loads_and_scorer_registered(suite_name: str) -> None:
    suite, suite_path = load_suite(REPO_ROOT, suite_name)
    assert suite.name == suite_name
    assert suite_path.exists()
    assert suite.scorer
    scorer = get_scorer(suite.scorer)
    assert callable(scorer)


@pytest.mark.parametrize("suite_name", SUITE_NAMES)
def test_suite_dataset_has_samples(suite_name: str) -> None:
    dataset_path = _dataset_path(suite_name)
    samples = load_dataset(dataset_path)
    assert len(samples) >= 1
    assert all(sample.suite == suite_name for sample in samples)


@pytest.mark.parametrize("suite_name", SUITE_NAMES)
def test_workflow_suite_uses_entrypoint_contract(suite_name: str) -> None:
    suite, _ = load_suite(REPO_ROOT, suite_name)
    if suite.executor.get("type") != "workflow-run":
        return
    assert "scope" not in suite.executor
    assert isinstance(suite.executor.get("entrypoint"), str)


@pytest.mark.parametrize("suite_name,tier", sorted(CODEGEN_SUITE_PENDING_TIERS.items()))
def test_codegen_datasets_use_pending_tiers(suite_name: str, tier: str) -> None:
    samples = load_dataset(_dataset_path(suite_name))
    assert samples
    for sample in samples:
        assert sample.input.get("fixture_tier") == tier


@pytest.mark.parametrize("suite_name", sorted(CODEGEN_SUITE_PENDING_TIERS))
def test_codegen_suites_hard_gate_current_metrics(suite_name: str) -> None:
    suite, _ = load_suite(REPO_ROOT, suite_name)
    assert suite.regression is not None
    for metric in CODEGEN_HARD_METRICS:
        policy = suite.regression.metrics[metric]
        assert policy.direction == "higher_is_better"
        assert policy.max_regression == 0.0
        thresholds = [t for t in suite.thresholds if t.metric == metric]
        assert len(thresholds) == 1
        threshold = thresholds[0]
        assert threshold.gate == "hard"
        assert threshold.op == "gte"
        assert threshold.value == 1.0


@pytest.mark.parametrize("suite_name", sorted(CODEGEN_SUITE_PENDING_TIERS))
def test_codegen_suite_zero_hard_metric_fails_verdict(suite_name: str) -> None:
    suite, _ = load_suite(REPO_ROOT, suite_name)
    metrics = {
        "evidence_integrity": 1.0,
        "schema_valid_rate": 1.0,
        "codegen_summary_present_rate": 1.0,
        "secret_leak_count": 0.0,
        "forbidden_write_executed_count": 0.0,
        "current_assurance_chain_rate": 1.0,
        "current_codegen_attempt_rate": 1.0,
        "selected_test_write_rate": 1.0,
    }
    metrics[CODEGEN_HARD_METRICS[0]] = 0.0
    result = compute_gate_result(
        suite,
        RunManifest(
            run_id="run-activation",
            suite=suite_name,
            scorer=suite.scorer,
            selected_sample_ids=["S-1"],
            total_samples=1,
            executed_samples=1,
            target_model="deterministic",
            started_at="2026-08-01T00:00:00Z",
        ),
        SuiteMetrics(run_id="run-activation", suite=suite_name, sample_count=1, metrics=metrics),
    )
    assert result.verdict == "fail"
    assert CODEGEN_HARD_METRICS[0] in result.hard_gate_failures
