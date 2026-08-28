from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

WORKFLOW_MODULE_ID = "assurance.quality.workflow"
WORKFLOW_RESOURCE_ID = "assurance.quality.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = (
    "assess",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "report",
)
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

_DOC_AUTHOR = "assurance-v1-doc-author"
_REPORTER = "assurance-v1-reporter"
_REVIEWER = "assurance-v1-reviewer"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(base: str, skill_id: str, agent_profile: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.quality.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("fact-baseline", "aa-fact-baseline", _DOC_AUTHOR, ("facts/fact-baseline.json",)),
    ("inspect", "aa-inspect", _REVIEWER, ("inspect/inspection.json",)),
    ("issue-analysis", "aa-issue-analyzer", _REPORTER, ("inspect/issue-analysis.json",)),
    ("issue-triage", "aa-issue-triage-advisor", _REPORTER, ("inspect/issue-triage.json",)),
    ("report", "aa-report-generator", _REPORTER, ("report/report.md",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {base: _job(base, skill_id, agent_profile, outputs) for base, skill_id, agent_profile, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, _profile, outputs in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
