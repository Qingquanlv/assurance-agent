from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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


def advance_repair_round(data: object) -> HealingRepairRoundAdvanceOutput:
    try:
        payload = HealingRepairRoundAdvanceInput.model_validate(data)
    except ValidationError:
        raise
    if payload.kind not in _KINDS:
        raise ValueError(f"unknown healing repair kind: {payload.kind}")
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return HealingRepairRoundAdvanceOutput(
        kind=payload.kind,
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )


__all__ = [
    "HealingRepairRoundAdvanceInput",
    "HealingRepairRoundAdvanceOutput",
    "RepairRoundKind",
    "advance_repair_round",
]
