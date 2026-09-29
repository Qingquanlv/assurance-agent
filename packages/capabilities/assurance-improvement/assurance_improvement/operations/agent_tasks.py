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

from assurance_improvement.contracts.agent import (
    ArchiveResultV1,
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    RetroAnalysisInputV1,
    RetroAnalysisResultV3,
    RetroSynthesisInputV1,
)
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS


@dataclass
class ArchiveTask:
    contract = AGENT_JOB_CONTRACTS["archive"]

    prepare_phase: PreparePhase[ImprovementSkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[ImprovementSkillInputV1, AgentRunRequest, ArchiveResultV1, ArchiveResultV1]

    @before
    async def prepare(
        self, validated_input: ImprovementSkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[ImprovementSkillInputV1, AgentRunRequest, ArchiveResultV1],
        scope: AuthorizedAttemptScope,
    ) -> ArchiveResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class ImprovementReviewTask:
    contract = AGENT_JOB_CONTRACTS["improvement-review"]

    prepare_phase: PreparePhase[ImprovementSkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        ImprovementSkillInputV1, AgentRunRequest, ImprovementReviewResultV1, ImprovementReviewResultV1
    ]

    @before
    async def prepare(
        self, validated_input: ImprovementSkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[ImprovementSkillInputV1, AgentRunRequest, ImprovementReviewResultV1],
        scope: AuthorizedAttemptScope,
    ) -> ImprovementReviewResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class RetroEvalAnalysisTask:
    contract = AGENT_JOB_CONTRACTS["retro-eval-analysis"]

    prepare_phase: PreparePhase[RetroAnalysisInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3, RetroAnalysisResultV3
    ]

    @before
    async def prepare(
        self, validated_input: RetroAnalysisInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3],
        scope: AuthorizedAttemptScope,
    ) -> RetroAnalysisResultV3 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class RetroIssueAnalysisTask:
    contract = AGENT_JOB_CONTRACTS["retro-issue-analysis"]

    prepare_phase: PreparePhase[RetroAnalysisInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3, RetroAnalysisResultV3
    ]

    @before
    async def prepare(
        self, validated_input: RetroAnalysisInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3],
        scope: AuthorizedAttemptScope,
    ) -> RetroAnalysisResultV3 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class RetroWorkflowAnalysisTask:
    contract = AGENT_JOB_CONTRACTS["retro-workflow-analysis"]

    prepare_phase: PreparePhase[RetroAnalysisInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3, RetroAnalysisResultV3
    ]

    @before
    async def prepare(
        self, validated_input: RetroAnalysisInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[RetroAnalysisInputV1, AgentRunRequest, RetroAnalysisResultV3],
        scope: AuthorizedAttemptScope,
    ) -> RetroAnalysisResultV3 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class RetroTask:
    contract = AGENT_JOB_CONTRACTS["retro"]

    prepare_phase: PreparePhase[RetroSynthesisInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        RetroSynthesisInputV1, AgentRunRequest, RetroAnalysisResultV3, RetroAnalysisResultV3
    ]

    @before
    async def prepare(
        self, validated_input: RetroSynthesisInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[RetroSynthesisInputV1, AgentRunRequest, RetroAnalysisResultV3],
        scope: AuthorizedAttemptScope,
    ) -> RetroAnalysisResultV3 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)
