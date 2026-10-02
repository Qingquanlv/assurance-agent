"""Case design: author the case delta, proposal, and minimum coverage matrix."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, Prepare

from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.handoff import PLAN
from assurance_intake.ops import router
from assurance_intake.ops.case_design import hooks
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1

op = router.agent(
    "case-design",
    input=CaseDesignInputV1,
    prepare=Prepare(hook=hooks.before, depends=(PLAN,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-design",
        writes=(
            Out("marker", MARKER_PATH),
            Out("proposal", PROPOSAL_PATH),
            Out("matrix", MATRIX_PATH),
            Dir("qa/cases", name="case", files=lambda business: business.case_delta_paths),
        ),
    ),
    finalize=Finalize(hook=hooks.after),
    output=CaseDesignOutputV1,
    retry=router.agent_retry.model_copy(update={"carry_invalid_output": True}),
)

__all__ = ["CaseDesignInputV1", "CaseDesignOutputV1", "op"]
