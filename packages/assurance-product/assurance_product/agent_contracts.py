from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from graph_engine.graph.schema import RetryPolicyDef, WorkflowDef
from graph_engine.plugin_api import FrozenModel, ResourceClaims

from assurance_product.models import PREPARE_IDS, alias_ids_for_prepare
from assurance_product.output_routes import OutputRouteCatalog, execute_alias_for_prepare


class AgentExecutionContract(FrozenModel):
    skill_id: str
    agent_profile: str
    resources: ResourceClaims


def _contract(
    skill_id: str,
    agent_profile: str,
    *,
    writes: tuple[str, ...],
) -> AgentExecutionContract:
    return AgentExecutionContract(
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaims(reads=("qa",), writes=writes),
    )


_CHANGE_WRITES = ("qa/changes",)
_TEST_WRITES = ("qa/changes", "tests")
_AGENT_RETRY_NAME = "agent-transient"
_AGENT_RETRY_POLICY = RetryPolicyDef(max_attempts=12, retry_on=("transient",))
_ARCHIVER_PROFILE = "assurance-v1-archiver"
_DOC_AUTHOR_PROFILE = "assurance-v1-doc-author"
_EXECUTOR_PROFILE = "assurance-v1-executor"
_EXPLORER_PROFILE = "assurance-v1-explorer"
_REPORTER_PROFILE = "assurance-v1-reporter"
_REVIEWER_PROFILE = "assurance-v1-reviewer"
_TEST_AUTHOR_PROFILE = "assurance-v1-test-author"

AGENT_EXECUTION_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        "assurance.intake.case-design.prepare": _contract(
            "aa-case-design", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.intake.case-review.prepare": _contract(
            "aa-case-reviewer", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.intake.explore.prepare": _contract("aa-explore", _EXPLORER_PROFILE, writes=_CHANGE_WRITES),
        "assurance.intake.intake.prepare": _contract("aa-intake", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES),
        "assurance.generation.api.codegen-fix.prepare": _contract(
            "aa-api-codegen-fixer", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.api.codegen.prepare": _contract(
            "aa-api-codegen", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.api.plan-review.prepare": _contract(
            "aa-api-plan-reviewer", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.api.plan.prepare": _contract(
            "aa-api-plan", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.e2e.codegen-fix.prepare": _contract(
            "aa-e2e-codegen-fixer", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.e2e.codegen.prepare": _contract(
            "aa-e2e-codegen", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.e2e.plan-review.prepare": _contract(
            "aa-e2e-plan-reviewer", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.e2e.plan.prepare": _contract(
            "aa-e2e-plan", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.fuzz.codegen.prepare": _contract(
            "aa-fuzz-codegen", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.fuzz.plan-review.prepare": _contract(
            "aa-fuzz-plan-reviewer", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.fuzz.plan.prepare": _contract(
            "aa-fuzz-plan", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.performance.codegen.prepare": _contract(
            "aa-performance-codegen", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.generation.performance.plan-review.prepare": _contract(
            "aa-performance-plan-reviewer", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.generation.performance.plan.prepare": _contract(
            "aa-performance-plan", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.execution.execute.prepare": _contract(
            "aa-execute", _EXECUTOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.execution.run.prepare": _contract("aa-run", _EXECUTOR_PROFILE, writes=_CHANGE_WRITES),
        "assurance.healing.coverage-repair.prepare": _contract(
            "aa-coverage-repair", _TEST_AUTHOR_PROFILE, writes=_TEST_WRITES
        ),
        "assurance.healing.fix-proposal.prepare": _contract(
            "aa-fix-proposal", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.quality.fact-baseline.prepare": _contract(
            "aa-fact-baseline", _DOC_AUTHOR_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.quality.inspect.prepare": _contract(
            "aa-inspect", _REVIEWER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.quality.issue-analysis.prepare": _contract(
            "aa-issue-analyzer", _REPORTER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.quality.issue-triage.prepare": _contract(
            "aa-issue-triage-advisor", _REPORTER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.quality.report.prepare": _contract(
            "aa-report-generator", _REPORTER_PROFILE, writes=_CHANGE_WRITES
        ),
        "assurance.improvement.archive.prepare": _contract(
            "aa-archive", _ARCHIVER_PROFILE, writes=("qa/archive", "qa/cases")
        ),
        "assurance.improvement.improvement-review.prepare": _contract(
            "aa-improvement-reviewer", _REVIEWER_PROFILE, writes=("qa/improvements",)
        ),
        "assurance.improvement.retro-eval-analysis.prepare": _contract(
            "aa-retro-eval-analysis", _DOC_AUTHOR_PROFILE, writes=("qa/retro",)
        ),
        "assurance.improvement.retro-issue-analysis.prepare": _contract(
            "aa-retro-issue-analysis", _DOC_AUTHOR_PROFILE, writes=("qa/retro",)
        ),
        "assurance.improvement.retro-workflow-analysis.prepare": _contract(
            "aa-retro-workflow-analysis", _DOC_AUTHOR_PROFILE, writes=("qa/retro",)
        ),
        "assurance.improvement.retro.prepare": _contract(
            "aa-retro", _DOC_AUTHOR_PROFILE, writes=("qa/retro",)
        ),
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
