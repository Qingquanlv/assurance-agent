"""Case design: author the case delta, proposal, and minimum coverage matrix."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, Prepare

from assurance_intake.contracts import CaseYamlAuthoring, MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.cases import QaYaml
from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.handoff import (
    CASE_API,
    CASE_CATALOG,
    CASE_EXPLORATION,
    CASE_INVENTORY,
    CASE_KNOWLEDGE,
    CASE_UI,
    PLAN,
    PREPARATION,
    REWORK_CONTEXT,
)
from assurance_intake.ops import router
from assurance_intake.ops.case_design import hooks
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.agent(
    "case-design",
    transport_business=True,
    input=CaseDesignInputV1,
    prepare=Prepare(
        hook=hooks.before,
        depends=(
            PLAN,
            PREPARATION,
            REWORK_CONTEXT,
            CASE_INVENTORY,
            CASE_EXPLORATION,
            CASE_UI,
            CASE_API,
            CASE_CATALOG,
            CASE_KNOWLEDGE,
        ),
        eager_artifacts=True,
    ),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-design",
        writes=(
            Out("marker", MARKER_PATH, model=QaYaml, format="yaml"),
            Out("proposal", PROPOSAL_PATH),
            Out("matrix", MATRIX_PATH, model=MinimumCoverageMatrixAuthoring, format="json"),
            Dir(
                "qa/cases",
                name="case",
                files=lambda business: business.case_delta_paths,
                model=CaseYamlAuthoring,
                format="yaml",
                context=lambda business: {
                    "capability_leafs": frozenset(getattr(business, "capability_leafs")),
                    "inventory": getattr(business, "impact_inventory"),
                },
            ),
        ),
        strict_files=True,
    ),
    finalize=Finalize(hook=hooks.after, artifacts="auto"),
    output=CaseDesignOutputV1,
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
    retry=router.agent_retry.model_copy(update={"carry_invalid_output": True}),
)

__all__ = ["CaseDesignInputV1", "CaseDesignOutputV1", "op"]
