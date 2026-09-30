from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

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
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_generation.contracts.codegen import CodegenAuthoringV1, CodegenResultV1
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.plugin import GenerationPlugin


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


@dataclass(frozen=True, slots=True)
class GenerationGraphs:
    generation: CompiledStateGraph
    api: CompiledStateGraph
    e2e: CompiledStateGraph
    fuzz: CompiledStateGraph
    performance: CompiledStateGraph
    init_runtime: CompiledStateGraph
    resolve_inputs: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=GenerationPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.generation", "assurance_generation.graphs.factory:build_generation_graphs"
    ),
    agent_task_types=(
        ApiCodegenTask,
        ApiCodegenReviewTask,
        E2ECodegenTask,
        E2ECodegenReviewTask,
        FuzzCodegenTask,
        FuzzCodegenReviewTask,
        PerformanceCodegenTask,
        PerformanceCodegenReviewTask,
    ),
)

__all__ = [
    "FEATURE",
    "GenerationGraphs",
    "ApiCodegenTask",
    "ApiCodegenReviewTask",
    "E2ECodegenTask",
    "E2ECodegenReviewTask",
    "FuzzCodegenTask",
    "FuzzCodegenReviewTask",
    "PerformanceCodegenTask",
    "PerformanceCodegenReviewTask",
]
