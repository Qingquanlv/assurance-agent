"""Deterministic Healing workflow-state handlers.

The typed graph calls ``advance_repair_round`` as an ordinary node. This
handler remains the YAML adapter and discards ``TaskContext``.
"""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, failed_input
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.decisions import (
    HealingRepairRoundAdvanceInput,
    HealingRepairRoundAdvanceOutput,
    RepairRoundKind,
    advance_repair_round,
)

REPAIR_ROUND_ADVANCE_ID = "assurance.healing.repair-round.advance"


class HealingRepairRoundAdvanceHandler:
    """Frozen increment of a failure or coverage repair counter."""

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            output = advance_repair_round(request.input)
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except (InputError, ValidationError, ValueError) as error:
            return failed_input(InputError(str(error)))


__all__ = [
    "HealingRepairRoundAdvanceHandler",
    "HealingRepairRoundAdvanceInput",
    "HealingRepairRoundAdvanceOutput",
    "REPAIR_ROUND_ADVANCE_ID",
    "RepairRoundKind",
]
