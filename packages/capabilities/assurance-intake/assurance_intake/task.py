"""Intake Agent tasks and their Product-facing feature bundle."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from agent_runtime_contracts import (
    AgentRunRequest,
    FinalizePhase,
    FinallyContext,
    PreparePhase,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    RuntimePhase,
    after,
    before,
    finally_,
)
from graph_engine.attempts import AuthorizedAttemptScope, PermanentTaskFailure
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_intake.contracts.agent import (
    ArtifactListResultV1,
    CaseDesignInputV1,
    CaseDesignOutputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    FinalizedArtifactsV1,
    IntakeInputV1,
)
from assurance_intake.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.plugin import IntakePlugin

logger = logging.getLogger(__name__)

CaseDesignBundle = RawFinalizeBundle[CaseDesignInputV1, AgentRunRequest, ArtifactListResultV1]


@dataclass
class IntakeTask:
    contract = AGENT_JOB_CONTRACTS["intake"]

    prepare_phase: PreparePhase[IntakeInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[IntakeInputV1, AgentRunRequest, ArtifactListResultV1, FinalizedArtifactsV1]

    @before
    async def prepare(
        self, validated_input: IntakeInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[IntakeInputV1, AgentRunRequest, ArtifactListResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedArtifactsV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class ExploreTask:
    contract = AGENT_JOB_CONTRACTS["explore"]

    prepare_phase: PreparePhase[ExploreInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[ExploreInputV1, AgentRunRequest, ArtifactListResultV1, FinalizedArtifactsV1]

    @before
    async def prepare(
        self, validated_input: ExploreInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[ExploreInputV1, AgentRunRequest, ArtifactListResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedArtifactsV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class CaseReviewTask:
    contract = AGENT_JOB_CONTRACTS["case-review"]

    prepare_phase: PreparePhase[CaseReviewInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CaseReviewInputV1, AgentRunRequest, CaseReviewResultV1, CaseReviewResultV1]

    @before
    async def prepare(
        self, validated_input: CaseReviewInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CaseReviewInputV1, AgentRunRequest, CaseReviewResultV1],
        scope: AuthorizedAttemptScope,
    ) -> CaseReviewResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class CaseDesignTask:
    contract = AGENT_JOB_CONTRACTS["case-design"]

    prepare_phase: PreparePhase[CaseDesignInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        CaseDesignInputV1, AgentRunRequest, ArtifactListResultV1, CaseDesignOutputV1
    ]

    @before
    async def prepare(
        self, validated_input: CaseDesignInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self, bundle: CaseDesignBundle, scope: AuthorizedAttemptScope
    ) -> CaseDesignOutputV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)

    @finally_
    async def on_exit(self, context: FinallyContext) -> None:
        logger.debug(
            "case-design segment exited: attempt=%s mode=%s outcome=%s",
            context.attempt_key,
            context.mode,
            context.outcome,
        )


@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    case: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=IntakePlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"
    ),
    agent_task_types=(IntakeTask, ExploreTask, CaseDesignTask, CaseReviewTask),
)

__all__ = [
    "FEATURE",
    "IntakeGraphs",
    "IntakeTask",
    "ExploreTask",
    "CaseDesignTask",
    "CaseReviewTask",
]
