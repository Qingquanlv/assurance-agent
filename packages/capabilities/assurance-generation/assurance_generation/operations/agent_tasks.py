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

from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_generation.contracts.codegen import CodegenAuthoringV1, CodegenResultV1
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring


@dataclass
class ApiCodegenTask:
    contract = AGENT_JOB_CONTRACTS["api.codegen"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1, CodegenResultV1]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1],
        scope: AuthorizedAttemptScope,
    ) -> CodegenResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class ApiCodegenReviewTask:
    contract = AGENT_JOB_CONTRACTS["api.codegen-review"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring, PlanReview]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring],
        scope: AuthorizedAttemptScope,
    ) -> PlanReview | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class E2ECodegenTask:
    contract = AGENT_JOB_CONTRACTS["e2e.codegen"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1, CodegenResultV1]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1],
        scope: AuthorizedAttemptScope,
    ) -> CodegenResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class E2ECodegenReviewTask:
    contract = AGENT_JOB_CONTRACTS["e2e.codegen-review"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring, PlanReview]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring],
        scope: AuthorizedAttemptScope,
    ) -> PlanReview | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class FuzzCodegenTask:
    contract = AGENT_JOB_CONTRACTS["fuzz.codegen"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1, CodegenResultV1]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1],
        scope: AuthorizedAttemptScope,
    ) -> CodegenResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class FuzzCodegenReviewTask:
    contract = AGENT_JOB_CONTRACTS["fuzz.codegen-review"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring, PlanReview]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring],
        scope: AuthorizedAttemptScope,
    ) -> PlanReview | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class PerformanceCodegenTask:
    contract = AGENT_JOB_CONTRACTS["performance.codegen"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1, CodegenResultV1]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, CodegenAuthoringV1],
        scope: AuthorizedAttemptScope,
    ) -> CodegenResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class PerformanceCodegenReviewTask:
    contract = AGENT_JOB_CONTRACTS["performance.codegen-review"]

    prepare_phase: PreparePhase[CodegenInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring, PlanReview]

    @before
    async def prepare(
        self, validated_input: CodegenInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CodegenInputV1, AgentRunRequest, PlanReviewAuthoring],
        scope: AuthorizedAttemptScope,
    ) -> PlanReview | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)
