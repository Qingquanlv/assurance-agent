"""Healing agent ops: one directory per op, dispatched through one execute entry."""

from __future__ import annotations

from agent_runtime_contracts.ops import AgentOpFinalizeInputV1, ArtifactHandle, OpRouter, Out
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

PROPOSAL_WRITE = Out("proposal", "qa/results/healing/fix-proposal.json")


def proposal_artifact(*, slot: str) -> ArtifactHandle[object]:
    """Handle for the proposal file ``fix-proposal`` writes. The key uses that write's name."""

    return ArtifactHandle(
        ledger_key=f"{router.owner.rsplit('.', 1)[-1]}.{PROPOSAL_WRITE.name}",
        slot=slot,
    )


router = OpRouter(
    "assurance.healing",
    package=__name__,
    reads=("qa",),
    agent_retry=AttemptRetryPolicy(max_attempts=10, interval_seconds=10),
    task_retry=AttemptRetryPolicy(max_attempts=1),
    timeout=AttemptTimeoutPolicy(seconds=60),
    scope=lambda business: business.change_id,
)
input_model = AgentOpFinalizeInputV1


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return await router.execute(request, context)


__all__ = ["PROPOSAL_WRITE", "execute", "input_model", "proposal_artifact", "router"]
