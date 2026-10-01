"""Explore: map the change's impact and draft the obligations it must cover."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.contracts.explore import EXPLORATION_PATH
from assurance_intake.ops import router
from assurance_intake.ops.explore import hooks
from assurance_intake.ops.explore.models import CONTEXT_PATH, EXPLORE_AGENT_OUTPUT_PATHS, ExploreInputV1

op = router.agent(
    "explore",
    input=ExploreInputV1,
    prepare=Prepare(hook=hooks.before, writes=(CONTEXT_PATH,), errors=(ValueError,)),
    agent=Agent(
        profile="assurance-v1-explorer",
        skill="aa-explore",
        writes=EXPLORE_AGENT_OUTPUT_PATHS,
    ),
    finalize=Finalize(hook=hooks.after, writes=(EXPLORATION_PATH,)),
    output=FinalizedArtifactsV1,
)

__all__ = ["ExploreInputV1", "op"]
