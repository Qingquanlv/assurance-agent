"""Case repair: apply a needs_fix review's bounded actions to the committed case-design outputs."""

from __future__ import annotations

from assurance_intake.domain.artifacts import ArtifactListResultV1
from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_repair import hooks
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, CaseRepairOutputV1

op = router.agent(
    "case-repair",
    profile="assurance-v1-doc-author",
    skill="aa-case-repair",
    input=CaseRepairInputV1,
    result=ArtifactListResultV1,
    output=CaseRepairOutputV1,
    writes=(MARKER_PATH, "qa/cases", PROPOSAL_PATH, MATRIX_PATH),
    routes=(MARKER_PATH, PROPOSAL_PATH, MATRIX_PATH),
    outputs=hooks.allowed_outputs,
    depends=(frozen_plan,),
    before=hooks.before,
    after=hooks.after,
)

__all__ = ["CaseRepairInputV1", "CaseRepairOutputV1", "op"]
