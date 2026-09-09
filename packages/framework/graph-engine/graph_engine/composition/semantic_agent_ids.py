from __future__ import annotations

SEMANTIC_AGENT_CONTRACT_IDS: frozenset[str] = frozenset(
    {
        "assurance.execution.agent.execute.v1",
        "assurance.execution.agent.run.v1",
        "assurance.generation.agent.api.codegen.v1",
        "assurance.generation.agent.api.plan-review.v1",
        "assurance.generation.agent.api.plan.v1",
        "assurance.generation.agent.e2e.codegen.v1",
        "assurance.generation.agent.e2e.plan-review.v1",
        "assurance.generation.agent.e2e.plan.v1",
        "assurance.generation.agent.fuzz.codegen.v1",
        "assurance.generation.agent.fuzz.plan-review.v1",
        "assurance.generation.agent.fuzz.plan.v1",
        "assurance.generation.agent.performance.codegen.v1",
        "assurance.generation.agent.performance.plan-review.v1",
        "assurance.generation.agent.performance.plan.v1",
        "assurance.healing.agent.apply-test-repair.v1",
        "assurance.healing.agent.coverage-repair.v1",
        "assurance.healing.agent.fix-proposal.v1",
        "assurance.improvement.agent.archive.v1",
        "assurance.improvement.agent.improvement-review.v1",
        "assurance.improvement.agent.retro-eval-analysis.v1",
        "assurance.improvement.agent.retro-issue-analysis.v1",
        "assurance.improvement.agent.retro-workflow-analysis.v1",
        "assurance.improvement.agent.retro.v1",
        "assurance.intake.agent.case-design.v1",
        "assurance.intake.agent.case-review.v1",
        "assurance.intake.agent.explore.v1",
        "assurance.intake.agent.intake.v1",
        "assurance.quality.agent.fact-baseline.v1",
        "assurance.quality.agent.inspect.v1",
        "assurance.quality.agent.issue-analysis.v1",
        "assurance.quality.agent.issue-triage.v1",
        "assurance.quality.agent.report.v1",
    }
)


def is_semantic_agent_contract_id(entry_id: str) -> bool:
    return entry_id in SEMANTIC_AGENT_CONTRACT_IDS


__all__ = [
    "SEMANTIC_AGENT_CONTRACT_IDS",
    "is_semantic_agent_contract_id",
]
