"""Apply test repair: edit the eligible generated tests and record the round."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, OutputError, Prepare
from agent_runtime_contracts.qa_paths import qa_route

from assurance_execution.contracts.workflow import APPLIED_REPAIR_PATH
from assurance_healing.contracts.application import (
    VERIFIED_REPAIR_PATH,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.repair_input import ApplyBoundInputV1
from assurance_healing.ops import proposal_artifact, router
from assurance_healing.ops.apply_test_repair import hooks

op = router.agent(
    "apply-test-repair",
    input=ApplyBoundInputV1,
    prepare=Prepare(
        hook=hooks.before,
        errors=(OutputError,),
        depends=(proposal_artifact(slot="proposal_ref"),),
    ),
    agent=Agent(
        profile="assurance-v1-test-author",
        skill="aa-apply-test-repair",
        result=TestRepairResultV1,
        writes=(Dir("qa/tests"),),
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(
            Out("verified-repair", VERIFIED_REPAIR_PATH),
            Out("applied-repair", APPLIED_REPAIR_PATH),
            *qa_route("healing/epochs"),
        ),
        same=("change_id",),
    ),
    output=VerifiedTestRepairV1,
)

__all__ = ["op"]
