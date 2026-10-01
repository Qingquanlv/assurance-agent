"""Intake: normalize the requirement into the change and write its marker."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.ops import router
from assurance_intake.ops.intake import hooks
from assurance_intake.ops.intake.models import IntakeInputV1

op = router.agent(
    "intake",
    input=IntakeInputV1,
    prepare=Prepare(hook=hooks.before, writes=(REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-intake",
        writes=(hooks.MARKER_PATH,),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FinalizedArtifactsV1,
)

__all__ = ["IntakeInputV1", "op"]
