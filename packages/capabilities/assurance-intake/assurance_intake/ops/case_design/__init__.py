"""Case design: author the case delta, proposal, and minimum coverage matrix."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Prepare

from assurance_intake.domain.artifacts import ArtifactListResultV1
from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.ops import router
from assurance_intake.ops.case_design import hooks
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1

op = router.agent(
    "case-design",
    input=CaseDesignInputV1,
    prepare=Prepare(hook=hooks.before, depends=(frozen_plan,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-design",
        result=ArtifactListResultV1,
        writes=(
            MARKER_PATH,
            PROPOSAL_PATH,
            MATRIX_PATH,
            Dir("qa/cases", files=lambda business: business.case_delta_paths),
        ),
    ),
    finalize=Finalize(hook=hooks.after, on_output_error=hooks.on_output_error),
    output=CaseDesignOutputV1,
)

__all__ = ["CaseDesignInputV1", "CaseDesignOutputV1", "op"]
