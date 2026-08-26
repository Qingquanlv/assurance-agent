from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from graph_engine.graph.schema import RetryPolicyDef, WorkflowDef
from graph_engine.plugin_api import FrozenModel, ResourceClaimTemplate, ResourceClaims

from assurance_product.models import PREPARE_IDS, alias_ids_for_prepare
from assurance_product.output_routes import OutputRouteCatalog, execute_alias_for_prepare


class AgentExecutionContract(FrozenModel):
    skill_id: str
    agent_profile: str
    resources: ResourceClaims | ResourceClaimTemplate


def _contract(
    prepare_id: str,
    skill_id: str,
    agent_profile: str,
) -> AgentExecutionContract:
    change_id = "CHANGE-ID-PLACEHOLDER"
    execute_alias = execute_alias_for_prepare(prepare_id)
    catalog = OutputRouteCatalog()
    exact_outputs = catalog.outputs(execute_alias, change_id)
    expected = catalog.resource_claims(execute_alias, change_id)
    marker = f"qa/changes/{change_id}/"
    writes = tuple(path.replace(marker, "qa/changes/{change_id}/", 1) for path in expected)
    if any(
        path == source or not source.startswith(marker) for path, source in zip(writes, expected, strict=True)
    ):
        raise ValueError(f"agent output route is not a current-change path: {execute_alias}")
    if any(path not in expected for path in exact_outputs):
        raise ValueError(f"agent output claim omits an exact output route: {execute_alias}")
    return AgentExecutionContract(
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=writes,
        ),
    )


_AGENT_RETRY_NAME = "agent-transient"
_AGENT_RETRY_POLICY = RetryPolicyDef(max_attempts=12, retry_on=("transient",))
_ARCHIVER_PROFILE = "assurance-v1-archiver"
_DOC_AUTHOR_PROFILE = "assurance-v1-doc-author"
_EXECUTOR_PROFILE = "assurance-v1-executor"
_EXPLORER_PROFILE = "assurance-v1-explorer"
_REPORTER_PROFILE = "assurance-v1-reporter"
_REVIEWER_PROFILE = "assurance-v1-reviewer"
_TEST_AUTHOR_PROFILE = "assurance-v1-test-author"

_AGENT_SKILL_PROFILES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "assurance.intake.case-design.prepare": ("aa-case-design", _DOC_AUTHOR_PROFILE),
        "assurance.intake.case-review.prepare": ("aa-case-reviewer", _REVIEWER_PROFILE),
        "assurance.intake.explore.prepare": ("aa-explore", _EXPLORER_PROFILE),
        "assurance.intake.intake.prepare": ("aa-intake", _DOC_AUTHOR_PROFILE),
        "assurance.generation.api.codegen-fix.prepare": ("aa-api-codegen-fixer", _TEST_AUTHOR_PROFILE),
        "assurance.generation.api.codegen.prepare": ("aa-api-codegen", _TEST_AUTHOR_PROFILE),
        "assurance.generation.api.plan-review.prepare": ("aa-api-plan-reviewer", _REVIEWER_PROFILE),
        "assurance.generation.api.plan.prepare": ("aa-api-plan", _DOC_AUTHOR_PROFILE),
        "assurance.generation.e2e.codegen-fix.prepare": ("aa-e2e-codegen-fixer", _TEST_AUTHOR_PROFILE),
        "assurance.generation.e2e.codegen.prepare": ("aa-e2e-codegen", _TEST_AUTHOR_PROFILE),
        "assurance.generation.e2e.plan-review.prepare": ("aa-e2e-plan-reviewer", _REVIEWER_PROFILE),
        "assurance.generation.e2e.plan.prepare": ("aa-e2e-plan", _DOC_AUTHOR_PROFILE),
        "assurance.generation.fuzz.codegen.prepare": ("aa-fuzz-codegen", _TEST_AUTHOR_PROFILE),
        "assurance.generation.fuzz.plan-review.prepare": ("aa-fuzz-plan-reviewer", _REVIEWER_PROFILE),
        "assurance.generation.fuzz.plan.prepare": ("aa-fuzz-plan", _DOC_AUTHOR_PROFILE),
        "assurance.generation.performance.codegen.prepare": ("aa-performance-codegen", _TEST_AUTHOR_PROFILE),
        "assurance.generation.performance.plan-review.prepare": (
            "aa-performance-plan-reviewer",
            _REVIEWER_PROFILE,
        ),
        "assurance.generation.performance.plan.prepare": ("aa-performance-plan", _DOC_AUTHOR_PROFILE),
        "assurance.execution.execute.prepare": ("aa-execute", _EXECUTOR_PROFILE),
        "assurance.execution.run.prepare": ("aa-run", _EXECUTOR_PROFILE),
        "assurance.healing.coverage-repair.prepare": ("aa-coverage-repair", _TEST_AUTHOR_PROFILE),
        "assurance.healing.fix-proposal.prepare": ("aa-fix-proposal", _DOC_AUTHOR_PROFILE),
        "assurance.quality.fact-baseline.prepare": ("aa-fact-baseline", _DOC_AUTHOR_PROFILE),
        "assurance.quality.inspect.prepare": ("aa-inspect", _REVIEWER_PROFILE),
        "assurance.quality.issue-analysis.prepare": ("aa-issue-analyzer", _REPORTER_PROFILE),
        "assurance.quality.issue-triage.prepare": ("aa-issue-triage-advisor", _REPORTER_PROFILE),
        "assurance.quality.report.prepare": ("aa-report-generator", _REPORTER_PROFILE),
        "assurance.improvement.archive.prepare": ("aa-archive", _ARCHIVER_PROFILE),
        "assurance.improvement.improvement-review.prepare": ("aa-improvement-reviewer", _REVIEWER_PROFILE),
        "assurance.improvement.retro-eval-analysis.prepare": ("aa-retro-eval-analysis", _DOC_AUTHOR_PROFILE),
        "assurance.improvement.retro-issue-analysis.prepare": (
            "aa-retro-issue-analysis",
            _DOC_AUTHOR_PROFILE,
        ),
        "assurance.improvement.retro-workflow-analysis.prepare": (
            "aa-retro-workflow-analysis",
            _DOC_AUTHOR_PROFILE,
        ),
        "assurance.improvement.retro.prepare": ("aa-retro", _DOC_AUTHOR_PROFILE),
    }
)

AGENT_EXECUTION_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        prepare_id: _contract(prepare_id, skill_id, agent_profile)
        for prepare_id, (skill_id, agent_profile) in _AGENT_SKILL_PROFILES.items()
    }
)


def bind_agent_execution_contracts(workflow: WorkflowDef) -> WorkflowDef:
    expected = set(PREPARE_IDS)
    actual = set(AGENT_EXECUTION_CONTRACTS)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"agent execution contract set drifted; missing={missing}, extra={extra}")
    catalog_aliases = set(OutputRouteCatalog().aliases())
    execute_aliases = {execute_alias_for_prepare(prepare_id) for prepare_id in PREPARE_IDS}
    if catalog_aliases != execute_aliases:
        missing = sorted(execute_aliases - catalog_aliases)
        extra = sorted(catalog_aliases - execute_aliases)
        raise ValueError(f"output route catalog drifted; missing={missing}, extra={extra}")

    retry = dict(workflow.retry)
    existing_retry = retry.get(_AGENT_RETRY_NAME)
    if existing_retry is not None and existing_retry != _AGENT_RETRY_POLICY:
        raise ValueError(f"workflow retry policy {_AGENT_RETRY_NAME!r} conflicts with agent contract")
    retry[_AGENT_RETRY_NAME] = _AGENT_RETRY_POLICY

    by_execute = {
        alias_ids_for_prepare(prepare_id)[1]: contract
        for prepare_id, contract in AGENT_EXECUTION_CONTRACTS.items()
    }
    seen: set[str] = set()
    graphs = {}
    for graph_id, graph in workflow.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            contract = by_execute.get(node.capability or "")
            if contract is None:
                nodes[node_id] = node
                continue
            if node.resources != ResourceClaims():
                raise ValueError(
                    f"agent execute node duplicates its execution contract: {graph_id}/{node_id}"
                )
            assert node.capability is not None
            seen.add(node.capability)
            nodes[node_id] = node.model_copy(
                update={"resources": contract.resources, "retry": _AGENT_RETRY_NAME}
            )
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})
    missing_execute = sorted(set(by_execute) - seen)
    if missing_execute:
        raise ValueError(f"workflow is missing contracted agent execute aliases: {missing_execute}")
    return workflow.model_copy(update={"graphs": graphs, "retry": retry})


__all__ = [
    "AGENT_EXECUTION_CONTRACTS",
    "AgentExecutionContract",
    "bind_agent_execution_contracts",
]
