"""Decision-support matrix: which (checkpoint, action) pairs are legitimate.

Port of TS ``decision_support.ts``. ``resolve_decision_support`` maps a decision
checkpoint + action to its consumer; unsupported combinations fail closed with an
``AaError`` so ``record_decision`` never records decisions nothing will consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.orchestration.schema import GateDef

# Special checkpoints that are neither gate ids nor graph nodes (TS ``SPECIAL_DECISION_SUPPORT``).
SPECIAL_DECISION_SUPPORT: dict[str, dict[str, str]] = {
    "healing.safety": {"accept_risk": "healing-safety", "stop": "terminal-status"},
    "execution.test-changes": {
        "allow_test_changes": "execution-test-changes",
        "stop": "terminal-status",
    },
    "bootstrap": {"skip_branch": "bootstrap-decision", "stop": "terminal-status"},
}

# Gate checkpoints support fix_and_proceed / accept_risk / stop.
WORKFLOW_DECISION_SUPPORT: dict[str, dict[str, str]] = {
    "fix_and_proceed": {"gate": "gate-decision"},
    "accept_risk": {"gate": "gate-decision"},
    "stop": {"gate": "terminal-status"},
}


class _SchemaWithGates(Protocol):
    gates: dict[str, GateDef]


@dataclass(frozen=True)
class DecisionSupport:
    """Where a supported decision is consumed (TS ``DecisionSupport``)."""

    consumer: str
    gate_id: str | None = None


def resolve_decision_support(schema: _SchemaWithGates, checkpoint: str, action: str) -> DecisionSupport:
    special = SPECIAL_DECISION_SUPPORT.get(checkpoint)
    if special is not None:
        consumer = special.get(action)
        if consumer is None:
            raise _unsupported(checkpoint, action)
        return DecisionSupport(consumer=consumer)

    gate = schema.gates.get(checkpoint)
    if gate is None:
        raise AaError(f"Unknown checkpoint '{checkpoint}'")
    support_map = WORKFLOW_DECISION_SUPPORT.get(action)
    if support_map is None:
        raise _unsupported(checkpoint, action)
    consumer = support_map.get("gate")
    if consumer is None:
        raise _unsupported(checkpoint, action)
    return DecisionSupport(consumer=consumer, gate_id=checkpoint)


def _unsupported(checkpoint: str, action: str) -> AaError:
    return AaError(f"Unsupported decision: checkpoint '{checkpoint}' does not support action '{action}'")
