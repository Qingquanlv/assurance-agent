"""performance codegen: seed locked tests, then author the family suite."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Prepare

from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.contracts.codegen import CodegenAuthoringV1, CodegenResultV1
from assurance_generation.ops import router
from assurance_generation.ops.performance_codegen import hooks

op = router.agent(
    "performance.codegen",
    input=CodegenInputV1,
    prepare=Prepare(hook=hooks.before, writes=("qa/tests",), request=hooks.request),
    agent=Agent(
        profile="assurance-v1-test-author",
        skill="aa-performance-codegen",
        result=CodegenAuthoringV1,
        writes=(
            "qa/results/codegen/performance-codegen-summary.md",
            "qa/results/codegen/performance-generated-files.json",
            Dir("qa/tests"),
        ),
    ),
    finalize=Finalize(hook=hooks.after),
    output=CodegenResultV1,
)

__all__ = ["op"]
