"""Resolve plan authenticates the explore outputs and freezes the plan artifacts."""

from __future__ import annotations

from graph_engine.plugin_api import TaskContext

from assurance_intake.contracts.plan import ResolvePlanInputV1, ResolvePlanOutputV1
from assurance_intake.ops.resolve_plan.hooks.artifacts import resolve_plan_artifact


def run(ctx: TaskContext, business: ResolvePlanInputV1) -> ResolvePlanOutputV1:
    return resolve_plan_artifact(business, project_root=ctx.project_root, write_root=ctx.write_root)
