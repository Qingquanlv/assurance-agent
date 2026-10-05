"""Resolve plan: freeze the selected test families and quality goal for the change."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts.ops import Dir, Out

from assurance_intake.contracts.plan import (
    PREPARATION_REFS_PATH,
    ResolvePlanInputV1,
    ResolvePlanOutputV1,
)
from assurance_intake.handoff import load_plan
from assurance_intake.ops import router
from assurance_intake.ops.resolve_plan import hooks
from assurance_intake.ops.resolve_plan.hooks.artifacts import RESOLVE_DEPENDS
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.task(
    "resolve-plan",
    input=ResolvePlanInputV1,
    output=ResolvePlanOutputV1,
    run=hooks.run,
    depends=RESOLVE_DEPENDS,
    reads=(".aa", "qa"),
    writes=(
        Out("exploration", "qa/results/explore/exploration.json"),
        Out("preparation-refs", PREPARATION_REFS_PATH),
        Dir("qa/results/plan", name="plan"),
    ),
    errors=(ValueError, ValidationError, OSError),
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)
PLAN = op.artifact("plan", slot="plan_ref", loader=load_plan, same=("change_id", "plan_digest"), many=False)

__all__ = ["ResolvePlanInputV1", "ResolvePlanOutputV1", "op"]
