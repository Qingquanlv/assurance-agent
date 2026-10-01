"""Archive: seal the change receipt against the locked quality report."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_improvement.contracts.agent import ArchiveResultV1, ImprovementSkillInputV1
from assurance_improvement.ops import router
from assurance_improvement.ops.archive import hooks

op = router.agent(
    "archive",
    input=ImprovementSkillInputV1,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-archiver",
        skill="aa-archive",
        result=ArchiveResultV1,
        writes=("qa/results/archive/archive-receipt.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=ArchiveResultV1,
)

__all__ = ["op"]
