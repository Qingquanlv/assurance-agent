from __future__ import annotations

from dataclasses import dataclass

from agent_runtime_contracts import (
    AgentRunRequest,
    FinalizePhase,
    PreparePhase,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    RuntimePhase,
    after,
    before,
)
from graph_engine.attempts import AuthorizedAttemptScope, PermanentTaskFailure

from assurance_intake.contracts.agent import (
    ArtifactListResultV1,
    CaseReviewInputV1,
    ExploreInputV1,
    FinalizedArtifactsV1,
    IntakeInputV1,
)
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_intake.contracts.review import CaseReviewResultV1


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
