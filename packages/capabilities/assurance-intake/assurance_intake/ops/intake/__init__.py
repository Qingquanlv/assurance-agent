"""Intake: normalize the requirement into the change and write its marker."""

from __future__ import annotations

from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.domain.artifacts import ArtifactListResultV1, FinalizedArtifactsV1
from assurance_intake.ops import router
from assurance_intake.ops.intake import hooks
from assurance_intake.ops.intake.models import IntakeInputV1

op = router.agent(
    "intake",
    profile="assurance-v1-doc-author",
    skill="aa-intake",
    persona="intake-host",
    input=IntakeInputV1,
    result=ArtifactListResultV1,
    output=FinalizedArtifactsV1,
    writes=(hooks.MARKER_PATH,),
    prepare_writes=(REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH),
    before=hooks.before,
    after=hooks.after,
)

__all__ = ["IntakeInputV1", "op"]
