"""Resolve plan: freeze the selected test families and quality goal for the change."""

from __future__ import annotations

from pydantic import ValidationError

from graph_engine.plugin_api import TaskContext

from assurance_intake.contracts.plan import ResolvePlanInputV1, ResolvePlanOutputV1
from assurance_intake.domain.plan_artifacts import resolve_plan_artifact
from assurance_intake.ops import router


def run(ctx: TaskContext, business: ResolvePlanInputV1) -> ResolvePlanOutputV1:
    return resolve_plan_artifact(business, project_root=ctx.project_root, write_root=ctx.write_root)


op = router.task(
    "resolve-plan",
    input=ResolvePlanInputV1,
    output=ResolvePlanOutputV1,
    run=run,
    reads=(".aa", "qa"),
    writes=("qa/results/explore/exploration.json", "qa/results/plan"),
    errors=(ValueError, ValidationError, OSError),
)

__all__ = ["ResolvePlanInputV1", "ResolvePlanOutputV1", "op"]
