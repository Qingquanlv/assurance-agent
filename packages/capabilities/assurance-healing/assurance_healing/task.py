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

from assurance_healing.contracts.agent import (
    CoverageRepairInputV1,
    FixProposalInputV1,
    FixProposalResultV1,
)
from assurance_healing.contracts.application import (
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_healing.contracts.coverage_repair import CoverageRepairStatus
from assurance_healing.plugin import HealingPlugin


@dataclass
class FixProposalTask:
    contract = AGENT_JOB_CONTRACTS["fix-proposal"]

    prepare_phase: PreparePhase[FixProposalInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        FixProposalInputV1, AgentRunRequest, FixProposalResultV1, FixProposalResultV1
    ]

    @before
    async def prepare(
        self, validated_input: FixProposalInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[FixProposalInputV1, AgentRunRequest, FixProposalResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FixProposalResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class ApplyTestRepairTask:
    contract = AGENT_JOB_CONTRACTS["apply-test-repair"]

    prepare_phase: PreparePhase[ApplyTestRepairInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        ApplyTestRepairInputV1, AgentRunRequest, TestRepairResultV1, VerifiedTestRepairV1
    ]

    @before
    async def prepare(
        self, validated_input: ApplyTestRepairInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[ApplyTestRepairInputV1, AgentRunRequest, TestRepairResultV1],
        scope: AuthorizedAttemptScope,
    ) -> VerifiedTestRepairV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class CoverageRepairTask:
    contract = AGENT_JOB_CONTRACTS["coverage-repair"]

    prepare_phase: PreparePhase[CoverageRepairInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        CoverageRepairInputV1, AgentRunRequest, CoverageRepairStatus, CoverageRepairStatus
    ]

    @before
    async def prepare(
        self, validated_input: CoverageRepairInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CoverageRepairInputV1, AgentRunRequest, CoverageRepairStatus],
        scope: AuthorizedAttemptScope,
    ) -> CoverageRepairStatus | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass(frozen=True, slots=True)
class HealingGraphs:
    repair_failure: CompiledStateGraph
    repair_coverage: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=HealingPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"
    ),
    agent_task_types=(FixProposalTask, ApplyTestRepairTask, CoverageRepairTask),
)

__all__ = [
    "FEATURE",
    "HealingGraphs",
    "FixProposalTask",
    "ApplyTestRepairTask",
    "CoverageRepairTask",
]
