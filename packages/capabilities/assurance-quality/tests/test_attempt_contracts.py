from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from agent_runtime_contracts import AgentExecutionContract
from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    FinalizedIssueAnalysisV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    QualitySkillInputV1,
    ReportResultV1,
)
from assurance_quality.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_quality.contracts.assessment import (
    AssessmentSkillInputV1,
    FactBaselineSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    FinalizedReportV1,
    ReportSkillInputV1,
)
from assurance_quality.plugin import QualityPlugin


def test_quality_owns_five_agent_contracts() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 5
    assert tuple(TASK_ATTEMPT_CONTRACTS) == ("materialize-assessment-inputs", "reconcile-issues")
    materialize = TASK_ATTEMPT_CONTRACTS["materialize-assessment-inputs"]
    reconcile = TASK_ATTEMPT_CONTRACTS["reconcile-issues"]
    assert reconcile.contract_id == "assurance.quality.reconcile-issues"
    assert reconcile.handler_id == "assurance.quality.reconcile-issues.execute"
    assert reconcile.resources.writes == ("qa/results/issues/snapshot.json",)
    assert materialize.contract_id == "assurance.quality.materialize-assessment-inputs"
    assert materialize.handler_id == "assurance.quality.materialize-assessment-inputs.execute"
    assert isinstance(materialize.resources, ResourceClaimTemplate)
    resolved = materialize.resources.resolve(
        {
            "coverage_epoch_token": "2",
            "reviewed_case": {"change_id": "CH-1", "coverage_epoch": 2},
            "execution": {"batch_id": "B-1"},
        }
    )
    assert resolved.reads == (
        ".aa/capability-catalog.json",
        ".aa/data-knowledge.yaml",
        ".aa/policy.yaml",
        "issues",
        "qa",
    )
    assert resolved.writes == (
        "qa/results/inspect/epochs/2/batches/B-1/coverage-gaps.json",
        "qa/results/inspect/epochs/2/batches/B-1/issue-evidence-manifest.json",
        "qa/results/inspect/epochs/2/batches/B-1/metrics.json",
        "qa/results/inspect/epochs/2/batches/B-1/observations.json",
        "qa/results/inspect/epochs/2/batches/B-1/trace-sufficiency.json",
        "qa/results/inspect/epochs/2/batches/B-1/trace.json",
    )
    expected = {
        "fact-baseline": ("aa-fact-baseline", "assurance-v1-doc-author", FactBaselineResultV1),
        "inspect": ("aa-inspect", "assurance-v1-reviewer", InspectionResultV1),
        "issue-analysis": ("aa-issue-analyzer", "assurance-v1-reporter", IssueAnalysisResultV1),
        "issue-triage": ("aa-issue-triage-advisor", "assurance-v1-reporter", IssueTriageResultV1),
        "report": ("aa-report-generator", "assurance-v1-reporter", ReportResultV1),
    }
    assert set(AGENT_JOB_CONTRACTS) == set(expected)
    for base, (skill_id, profile, result_model) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert isinstance(contract, AgentExecutionContract)
        assert contract.contract_id == f"assurance.quality.agent.{base}.v1"
        assert contract.owner_id == "assurance.quality"
        assert contract.prepare_handler_id == f"assurance.quality.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.quality.{base}.finalize"
        claims = contract.phase_write_claims
        assert set(claims.runtime) == set(contract.resources.writes)
        assert contract.skill_id == skill_id
        assert contract.agent_profile == profile
        expected_input = {
            "fact-baseline": FactBaselineSkillInputV1,
            "inspect": AssessmentSkillInputV1,
            "report": ReportSkillInputV1,
        }.get(base, QualitySkillInputV1)
        expected_output = {
            "fact-baseline": FinalizedFactBaselineV1,
            "inspect": FinalizedInspectionV1,
            "issue-analysis": FinalizedIssueAnalysisV1,
            "report": FinalizedReportV1,
        }.get(base, result_model)
        assert contract.input_model is expected_input
        assert contract.agent_result_model is result_model
        assert contract.output_model is expected_output
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60


def test_quality_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == QualityPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert len(contribution.commit_validators) == 6
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
