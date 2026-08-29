"""Deterministic Healing workflow-state handlers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.operations.common import InputError, failed_input

REPAIR_ROUND_ADVANCE_ID = "assurance.healing.repair-round.advance"
RepairRoundKind = Literal["failure", "coverage"]
_KINDS: tuple[RepairRoundKind, ...] = ("failure", "coverage")


class HealingRepairRoundAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RepairRoundKind
    rounds_used: int = Field(ge=0)
    rounds_budget: int = Field(ge=1)


class HealingRepairRoundAdvanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RepairRoundKind
    rounds_used: int = Field(ge=1)
    rounds_budget: int = Field(ge=1)


class HealingRepairRoundAdvanceHandler:
    """Frozen increment of a failure or coverage repair counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = _validate(request.input)
            if payload.kind not in _KINDS:
                raise InputError(f"unknown healing repair kind: {payload.kind}")
            if payload.rounds_used >= payload.rounds_budget:
                raise InputError("rounds_used must be below rounds_budget")
            output = HealingRepairRoundAdvanceOutput(
                kind=payload.kind,
                rounds_used=payload.rounds_used + 1,
                rounds_budget=payload.rounds_budget,
            )
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)


def _validate(data: object) -> HealingRepairRoundAdvanceInput:
    try:
        return HealingRepairRoundAdvanceInput.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
