"""Case repair: apply a needs_fix review's bounded actions to the committed case-design outputs."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Prepare

from assurance_intake.domain.artifacts import ArtifactListResultV1
from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_repair import hooks
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, CaseRepairOutputV1

op = router.agent(
    "case-repair",
    input=CaseRepairInputV1,
    prepare=Prepare(hook=hooks.before, depends=(frozen_plan,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-repair",
        result=ArtifactListResultV1,
        writes=(
            MARKER_PATH,
            PROPOSAL_PATH,
            MATRIX_PATH,
            Dir("qa/cases", files=lambda business: business.case_delta_paths),
        ),
    ),
    finalize=Finalize(hook=hooks.after),
    output=CaseRepairOutputV1,
)

__all__ = ["CaseRepairInputV1", "CaseRepairOutputV1", "op"]
