from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.mark.parametrize(
    ("name", "contract_id"),
    (
        ("intake", "assurance.intake.agent.intake.v1"),
        ("explore", "assurance.intake.agent.explore.v1"),
        ("case-design", "assurance.intake.agent.case-design.v1"),
        ("case-review", "assurance.intake.agent.case-review.v1"),
        ("api.codegen", "assurance.generation.agent.api.codegen.v1"),
        ("api.codegen-review", "assurance.generation.agent.api.codegen-review.v1"),
        ("e2e.codegen", "assurance.generation.agent.e2e.codegen.v1"),
        ("e2e.codegen-review", "assurance.generation.agent.e2e.codegen-review.v1"),
        ("fuzz.codegen", "assurance.generation.agent.fuzz.codegen.v1"),
        ("fuzz.codegen-review", "assurance.generation.agent.fuzz.codegen-review.v1"),
        ("performance.codegen", "assurance.generation.agent.performance.codegen.v1"),
        ("performance.codegen-review", "assurance.generation.agent.performance.codegen-review.v1"),
        ("coverage-repair", "assurance.healing.agent.coverage-repair.v1"),
        ("fix-proposal", "assurance.healing.agent.fix-proposal.v1"),
        ("apply-test-repair", "assurance.healing.agent.apply-test-repair.v1"),
        ("fact-baseline", "assurance.quality.agent.fact-baseline.v1"),
        ("inspect", "assurance.quality.agent.inspect.v1"),
        ("issue-analysis", "assurance.quality.agent.issue-analysis.v1"),
        ("issue-triage", "assurance.quality.agent.issue-triage.v1"),
        ("report", "assurance.quality.agent.report.v1"),
        ("archive", "assurance.improvement.agent.archive.v1"),
        ("improvement-review", "assurance.improvement.agent.improvement-review.v1"),
        ("retro-eval-analysis", "assurance.improvement.agent.retro-eval-analysis.v1"),
        ("retro-issue-analysis", "assurance.improvement.agent.retro-issue-analysis.v1"),
        ("retro-workflow-analysis", "assurance.improvement.agent.retro-workflow-analysis.v1"),
        ("retro", "assurance.improvement.agent.retro.v1"),
    ),
)
def test_agent_task_phases_delegate_to_injected_phases(name: str, contract_id: str) -> None:
    from agent_runtime_contracts.lifecycle import validate_task_type

    from assurance_intake.task import CaseDesignTask, CaseReviewTask, ExploreTask, IntakeTask
    from assurance_generation.operations.agent_tasks import (
        ApiCodegenReviewTask,
        ApiCodegenTask,
        E2ECodegenReviewTask,
        E2ECodegenTask,
        FuzzCodegenReviewTask,
        FuzzCodegenTask,
        PerformanceCodegenReviewTask,
        PerformanceCodegenTask,
    )
    from assurance_healing.operations.agent_tasks import (
        ApplyTestRepairTask,
        CoverageRepairTask,
        FixProposalTask,
    )
    from assurance_quality.operations.agent_tasks import (
        FactBaselineTask,
        InspectTask,
        IssueAnalysisTask,
        IssueTriageTask,
        ReportTask,
    )
    from assurance_improvement.operations.agent_tasks import (
        ArchiveTask,
        ImprovementReviewTask,
        RetroEvalAnalysisTask,
        RetroIssueAnalysisTask,
        RetroTask,
        RetroWorkflowAnalysisTask,
    )

    task_type = {
        "intake": IntakeTask,
        "explore": ExploreTask,
        "case-design": CaseDesignTask,
        "case-review": CaseReviewTask,
        "api.codegen": ApiCodegenTask,
        "api.codegen-review": ApiCodegenReviewTask,
        "e2e.codegen": E2ECodegenTask,
        "e2e.codegen-review": E2ECodegenReviewTask,
        "fuzz.codegen": FuzzCodegenTask,
        "fuzz.codegen-review": FuzzCodegenReviewTask,
        "performance.codegen": PerformanceCodegenTask,
        "performance.codegen-review": PerformanceCodegenReviewTask,
        "coverage-repair": CoverageRepairTask,
        "fix-proposal": FixProposalTask,
        "apply-test-repair": ApplyTestRepairTask,
        "fact-baseline": FactBaselineTask,
        "inspect": InspectTask,
        "issue-analysis": IssueAnalysisTask,
        "issue-triage": IssueTriageTask,
        "report": ReportTask,
        "archive": ArchiveTask,
        "improvement-review": ImprovementReviewTask,
        "retro-eval-analysis": RetroEvalAnalysisTask,
        "retro-issue-analysis": RetroIssueAnalysisTask,
        "retro-workflow-analysis": RetroWorkflowAnalysisTask,
        "retro": RetroTask,
    }[name]
    calls: list[tuple[str, object, object]] = []

    class Phase:
        def __init__(self, name: str) -> None:
            self.name = name

        async def execute(self, value: object, scope: object) -> object:
            calls.append((self.name, value, scope))
            return self.name

    validate_task_type(task_type)
    assert task_type.contract.contract_id == contract_id
    task = task_type(
        prepare_phase=Phase("prepare"),
        opencode=Phase("runtime"),
        finalize_phase=Phase("finalize"),
    )
    scope = object()
    input_value = object()
    prepared = object()
    bundle = object()

    async def run_phases() -> tuple[object, object, object]:
        return (
            await task.prepare(input_value, scope),
            await task.run(prepared, scope),
            await task.finalize(bundle, scope),
        )

    assert asyncio.run(run_phases()) == ("prepare", "runtime", "finalize")
    assert calls == [
        ("prepare", input_value, scope),
        ("runtime", prepared, scope),
        ("finalize", bundle, scope),
    ]
