from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from assurance_agent.eval.scorers import codegen, workflow_case, workflow_full, workflow_run
from assurance_agent.eval.types import DatasetSample, SampleScore

Scorer = Callable[[DatasetSample, Path], SampleScore]

_CODEGEN_SUITES = (
    "workflow-api-codegen",
    "workflow-e2e-codegen",
    "workflow-fuzz-codegen",
    "workflow-performance-codegen",
)

_REGISTRY: dict[str, Scorer] = {
    "workflow-case": workflow_case.score,
    "workflow-run": workflow_run.score,
    "workflow-full": workflow_full.score,
    **{name: codegen.make_scorer(name) for name in _CODEGEN_SUITES},
}


def get_scorer(suite_name: str) -> Scorer:
    if suite_name not in _REGISTRY:
        raise KeyError(f"no scorer registered for suite: {suite_name}")
    return _REGISTRY[suite_name]
