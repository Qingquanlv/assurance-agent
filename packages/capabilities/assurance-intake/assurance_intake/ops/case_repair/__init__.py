"""Case repair: apply a needs_fix review's bounded actions to the committed case-design outputs."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, Prepare

from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.handoff import PLAN
from assurance_intake.ops import router
from assurance_intake.ops.case_repair import hooks
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, CaseRepairOutputV1

op = router.agent(
    "case-repair",
    input=CaseRepairInputV1,
    prepare=Prepare(hook=hooks.before, depends=(PLAN,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-repair",
        writes=(
            Out("marker", MARKER_PATH),
            Out("proposal", PROPOSAL_PATH),
            Out("matrix", MATRIX_PATH),
            Dir("qa/cases", name="case", files=lambda business: business.case_delta_paths),
        ),
    ),
    finalize=Finalize(hook=hooks.after),
    output=CaseRepairOutputV1,
)

__all__ = ["CaseRepairInputV1", "CaseRepairOutputV1", "op"]
