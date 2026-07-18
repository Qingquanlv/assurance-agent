"""Decision-support matrix: which (checkpoint, action) pairs are legitimate.

Port of TS ``decision_support.ts``. ``resolve_decision_support`` maps a decision
checkpoint + action to its consumer; unsupported combinations fail closed with an
``AaError`` so ``record_decision`` never records decisions nothing will consume.
"""

from __future__ import annotations

from dataclasses import dataclass

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.schema import WorkflowSchema

# Special checkpoints that are neither gate ids nor phases (TS ``SPECIAL_DECISION_SUPPORT``).
SPECIAL_DECISION_SUPPORT: dict[str, dict[str, str]] = {
    "healing.safety": {"accept_risk": "healing-safety", "stop": "terminal-status"},
    "execution.test-changes": {
        "allow_test_changes": "execution-test-changes",
        "stop": "terminal-status",
    },
    "bootstrap": {"skip_branch": "bootstrap-decision", "stop": "terminal-status"},
}

# Bare phases only support ``stop``; gate actions require a gated checkpoint
# (TS ``WORKFLOW_DECISION_SUPPORT``).
WORKFLOW_DECISION_SUPPORT: dict[str, dict[str, str]] = {
    "fix_and_proceed": {"gate": "gate-decision", "gated_phase": "gate-decision"},
    "accept_risk": {"gate": "gate-decision", "gated_phase": "gate-decision"},
    "stop": {
        "gate": "terminal-status",
        "gated_phase": "terminal-status",
        "phase": "terminal-status",
    },
}


@dataclass(frozen=True)
class DecisionSupport:
    """Where a supported decision is consumed (TS ``DecisionSupport``)."""

    consumer: str
    gate_id: str | None = None


def resolve_decision_support(schema: WorkflowSchema, checkpoint: str, action: str) -> DecisionSupport:
    special = SPECIAL_DECISION_SUPPORT.get(checkpoint)
    if special is not None:
        consumer = special.get(action)
        if consumer is None:
            raise _unsupported(checkpoint, action)
        return DecisionSupport(consumer=consumer)

    phase = next((p for p in schema.phases if p.id == checkpoint), None)
    gate = schema.gates.get(checkpoint)
    if phase is None and gate is None:
        raise AaError(f"Unknown checkpoint '{checkpoint}'")
    support_map = WORKFLOW_DECISION_SUPPORT.get(action)
    if support_map is None:
        raise _unsupported(checkpoint, action)

    if gate is not None:
        kind = "gate"
        gate_id = checkpoint
    elif phase is not None and phase.gate:
        kind = "gated_phase"
        gate_id = phase.gate
    else:
        kind = "phase"
        gate_id = None
    consumer = support_map.get(kind)
    if consumer is None:
        raise _unsupported(checkpoint, action)
    return DecisionSupport(consumer=consumer, gate_id=gate_id)


def _unsupported(checkpoint: str, action: str) -> AaError:
    return AaError(f"Unsupported decision: checkpoint '{checkpoint}' does not support action '{action}'")
