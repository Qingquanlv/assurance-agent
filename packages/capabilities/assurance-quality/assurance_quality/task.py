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

from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    FinalizedIssueAnalysisV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    QualitySkillInputV1,
    ReportResultV1,
)
from assurance_quality.contracts.assessment import (
    AssessmentSkillInputV1,
    FactBaselineSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    FinalizedReportV1,
    ReportSkillInputV1,
)
from assurance_quality.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_quality.plugin import QualityPlugin


@dataclass
class FactBaselineTask:
    contract = AGENT_JOB_CONTRACTS["fact-baseline"]

    prepare_phase: PreparePhase[FactBaselineSkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        FactBaselineSkillInputV1, AgentRunRequest, FactBaselineResultV1, FinalizedFactBaselineV1
    ]

    @before
    async def prepare(
        self, validated_input: FactBaselineSkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[FactBaselineSkillInputV1, AgentRunRequest, FactBaselineResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedFactBaselineV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class InspectTask:
    contract = AGENT_JOB_CONTRACTS["inspect"]

    prepare_phase: PreparePhase[AssessmentSkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        AssessmentSkillInputV1, AgentRunRequest, InspectionResultV1, FinalizedInspectionV1
    ]

    @before
    async def prepare(
        self, validated_input: AssessmentSkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[AssessmentSkillInputV1, AgentRunRequest, InspectionResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedInspectionV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class IssueAnalysisTask:
    contract = AGENT_JOB_CONTRACTS["issue-analysis"]

    prepare_phase: PreparePhase[QualitySkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        QualitySkillInputV1, AgentRunRequest, IssueAnalysisResultV1, FinalizedIssueAnalysisV1
    ]

    @before
    async def prepare(
        self, validated_input: QualitySkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[QualitySkillInputV1, AgentRunRequest, IssueAnalysisResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedIssueAnalysisV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class IssueTriageTask:
    contract = AGENT_JOB_CONTRACTS["issue-triage"]

    prepare_phase: PreparePhase[QualitySkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        QualitySkillInputV1, AgentRunRequest, IssueTriageResultV1, IssueTriageResultV1
    ]

    @before
    async def prepare(
        self, validated_input: QualitySkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[QualitySkillInputV1, AgentRunRequest, IssueTriageResultV1],
        scope: AuthorizedAttemptScope,
    ) -> IssueTriageResultV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass
class ReportTask:
    contract = AGENT_JOB_CONTRACTS["report"]

    prepare_phase: PreparePhase[ReportSkillInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[ReportSkillInputV1, AgentRunRequest, ReportResultV1, FinalizedReportV1]

    @before
    async def prepare(
        self, validated_input: ReportSkillInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[ReportSkillInputV1, AgentRunRequest, ReportResultV1],
        scope: AuthorizedAttemptScope,
    ) -> FinalizedReportV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


@dataclass(frozen=True, slots=True)
class QualityGraphs:
    assess: CompiledStateGraph
    issue_review: CompiledStateGraph
    issue_analyze: CompiledStateGraph
    issue_reconcile: CompiledStateGraph
    report: CompiledStateGraph
    fact_baseline: CompiledStateGraph
    surface_baseline: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=QualityPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"
    ),
    agent_task_types=(FactBaselineTask, InspectTask, IssueTriageTask, IssueAnalysisTask, ReportTask),
)

__all__ = [
    "FEATURE",
    "QualityGraphs",
    "FactBaselineTask",
    "InspectTask",
    "IssueAnalysisTask",
    "IssueTriageTask",
    "ReportTask",
]
