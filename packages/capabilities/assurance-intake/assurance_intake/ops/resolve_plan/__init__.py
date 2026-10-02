"""Resolve plan: freeze the selected test families and quality goal for the change."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts.ops import Dir

from assurance_intake.contracts.plan import ResolvePlanInputV1, ResolvePlanOutputV1
from assurance_intake.handoff import load_plan, plan_matches_case
from assurance_intake.ops import router
from assurance_intake.ops.resolve_plan import hooks

op = router.task(
    "resolve-plan",
    input=ResolvePlanInputV1,
    output=ResolvePlanOutputV1,
    run=hooks.run,
    reads=(".aa", "qa"),
    writes=(
        "qa/results/explore/exploration.json",
        Dir("qa/results/plan", name="plan"),
    ),
    errors=(ValueError, ValidationError, OSError),
)
PLAN = op.artifact("plan", slot="plan_ref", loader=load_plan, check=plan_matches_case, many=False)

__all__ = ["ResolvePlanInputV1", "ResolvePlanOutputV1", "op"]
