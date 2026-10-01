"""Apply test repair: edit the approved generated tests and record the round."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, OutputError, Prepare
from agent_runtime_contracts.qa_paths import qa_route

from assurance_healing.contracts.application import (
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.ops import router
from assurance_healing.ops.apply_test_repair import hooks

op = router.agent(
    "apply-test-repair",
    input=ApplyTestRepairInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-test-author",
        skill="aa-apply-test-repair",
        result=TestRepairResultV1,
        writes=(Dir("qa/tests"),),
    ),
    finalize=Finalize(hook=hooks.after, writes=qa_route("healing/epochs")),
    output=VerifiedTestRepairV1,
)

__all__ = ["op"]
