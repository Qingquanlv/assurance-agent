"""Coverage repair: close briefed gaps by editing the named test files."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_healing.contracts.agent import CoverageRepairInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary
from assurance_healing.ops import router
from assurance_healing.ops.coverage_repair import hooks

op = router.agent(
    "coverage-repair",
    input=CoverageRepairInputV1,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-test-author",
        skill="aa-coverage-repair",
        result=CoverageRepairApplySummary,
        writes=("qa/results/healing/coverage-repair.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=CoverageRepairApplySummary,
)

__all__ = ["op"]
