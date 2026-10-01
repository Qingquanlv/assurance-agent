"""Explore: map the change's impact and draft the obligations it must cover."""

from __future__ import annotations

from assurance_intake.contracts.explore import EXPLORATION_PATH, EXPLORE_AGENT_OUTPUT_PATHS
from assurance_intake.domain.artifacts import ArtifactListResultV1, FinalizedArtifactsV1
from assurance_intake.ops import router
from assurance_intake.ops.explore import hooks
from assurance_intake.ops.explore.models import ExploreInputV1

op = router.agent(
    "explore",
    profile="assurance-v1-explorer",
    skill="aa-explore",
    input=ExploreInputV1,
    result=ArtifactListResultV1,
    output=FinalizedArtifactsV1,
    writes=EXPLORE_AGENT_OUTPUT_PATHS,
    prepare_writes=(hooks.CONTEXT_PATH,),
    finalize_writes=(EXPLORATION_PATH,),
    before=hooks.before,
    after=hooks.after,
    input_errors=(ValueError,),
)

__all__ = ["ExploreInputV1", "op"]
