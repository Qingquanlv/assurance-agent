"""Fix proposal: propose an eligible repair against the locked issue analysis."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, OutputError, Prepare

from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.repair_input import RepairBoundInputV1
from assurance_healing.ops import router
from assurance_healing.ops.fix_proposal import hooks

op = router.agent(
    "fix-proposal",
    input=RepairBoundInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-fix-proposal",
        result=FixProposalResultV1,
        writes=(Out("proposal", hooks.PATH, model=FixProposalResultV1, format="json"),),
    ),
    finalize=Finalize(hook=hooks.after, same=("change_id",)),
    output=FixProposalResultV1,
)

__all__ = ["op"]
