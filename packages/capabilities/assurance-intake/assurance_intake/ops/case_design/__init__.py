"""Case design: author the case delta, proposal, and minimum coverage matrix."""

from __future__ import annotations

from assurance_intake.domain.artifacts import ArtifactListResultV1
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_design import hooks
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1

op = router.agent(
    "case-design",
    profile="assurance-v1-doc-author",
    skill="aa-case-design",
    input=CaseDesignInputV1,
    result=ArtifactListResultV1,
    output=CaseDesignOutputV1,
    writes=(hooks.MARKER_PATH, "qa/cases", hooks.PROPOSAL_PATH, hooks.MATRIX_PATH),
    routes=(hooks.MARKER_PATH, hooks.PROPOSAL_PATH, hooks.MATRIX_PATH),
    outputs=hooks.allowed_outputs,
    skills={"repair": "aa-case-repair"},
    depends=(frozen_plan,),
    before=hooks.before,
    after=hooks.after,
    on_output_error=hooks.on_output_error,
)

__all__ = ["CaseDesignInputV1", "CaseDesignOutputV1", "op"]
