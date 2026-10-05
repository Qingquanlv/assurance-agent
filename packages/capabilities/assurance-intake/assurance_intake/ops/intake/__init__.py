"""Intake: normalize the requirement into the change and write its marker."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, Prepare

from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.ops import router
from assurance_intake.ops.intake import hooks
from assurance_intake.ops.intake.models import IntakeInputV1, IntakeQaV1
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.agent(
    "intake",
    transport_business=True,
    input=IntakeInputV1,
    prepare=Prepare(
        hook=hooks.before,
        writes=(Out("requirement", REQUIREMENT_PATH), Out("run_spec", RUN_SPEC_SNAPSHOT_PATH)),
    ),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-intake",
        writes=(Out("marker", hooks.MARKER_PATH, model=IntakeQaV1, format="yaml"),),
        strict_files=True,
    ),
    finalize=Finalize(hook=hooks.after, artifacts="auto"),
    output=FinalizedArtifactsV1,
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)

__all__ = ["IntakeInputV1", "op"]
