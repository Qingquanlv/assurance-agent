from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.eval.dataset_loader import load_dataset
from assurance_agent.eval.paths import datasets_dir, eval_root
from assurance_agent.eval.plan import load_suite
from assurance_agent.eval.scorers import get_scorer

SUITE_NAMES = [
    "_test",
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
    if suite_name == "_test":
        return eval_root(REPO_ROOT) / "datasets" / "_test"
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
