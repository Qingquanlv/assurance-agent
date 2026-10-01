"""Fix proposal: propose an eligible repair against the locked issue analysis."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, OutputError, Prepare

from assurance_healing.contracts.agent import FixProposalInputV1, FixProposalResultV1
from assurance_healing.ops import router
from assurance_healing.ops.fix_proposal import hooks

op = router.agent(
    "fix-proposal",
    input=FixProposalInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-fix-proposal",
        result=FixProposalResultV1,
        writes=("qa/results/healing/fix-proposal.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FixProposalResultV1,
)

__all__ = ["op"]
