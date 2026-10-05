"""Explore: map the change's impact and draft the obligations it must cover."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, Prepare

from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.contracts.explore import (
    EXPLORATION_PATH,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    ExploreAdvisoryV1,
)
from assurance_intake.contracts.impact import INVENTORY_PATH, ChangeImpactInventoryV1
from assurance_intake.ops import router
from assurance_intake.ops.explore import hooks
from assurance_intake.ops.explore.models import (
    CHANGE_EVIDENCE_PATH,
    CONTEXT_PATH,
    EXPLORATION_DRAFT_PATH,
    ChangeEvidenceV1,
    ExploreContextV1,
    ExploreInputV1,
)
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.agent(
    "explore",
    transport_business=True,
    input=ExploreInputV1,
    prepare=Prepare(
        hook=hooks.before,
        writes=(Out("context", CONTEXT_PATH, model=ExploreContextV1, format="json"),),
        reads=(
            Out("requirement", REQUIREMENT_PATH, optional=True, input_source="stage_first"),
            Out("run_spec", RUN_SPEC_SNAPSHOT_PATH, optional=True, input_source="stage_first"),
            Out(
                "change_evidence", CHANGE_EVIDENCE_PATH, model=ChangeEvidenceV1, format="json", optional=True
            ),
        ),
        errors=(ValueError,),
    ),
    agent=Agent(
        profile="assurance-v1-explorer",
        skill="aa-explore",
        writes=(
            Out("draft", EXPLORATION_DRAFT_PATH, model=ExploreAdvisoryV1, format="json"),
            Out("inventory", INVENTORY_PATH, model=ChangeImpactInventoryV1, format="json"),
        ),
        strict_files=True,
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(Out("exploration", EXPLORATION_PATH),),
        errors=(ValueError,),
        error_failure="output",
        artifacts="auto",
    ),
    output=FinalizedArtifactsV1,
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)

__all__ = ["ExploreInputV1", "op"]
