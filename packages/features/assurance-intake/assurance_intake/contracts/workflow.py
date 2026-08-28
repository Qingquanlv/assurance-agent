from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

WORKFLOW_MODULE_ID = "assurance.intake.workflow"
WORKFLOW_RESOURCE_ID = "assurance.intake.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("prepare", "case")
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

_DOC_AUTHOR = "assurance-v1-doc-author"
_EXPLORER = "assurance-v1-explorer"
_REVIEWER = "assurance-v1-reviewer"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    outputs: tuple[str, ...],
    extra_claims: tuple[str, ...] = (),
) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.intake.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs, *extra_claims),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "case-design",
        "aa-case-design",
        _DOC_AUTHOR,
        (".qa.yaml", "proposal.md", "trace/minimum-coverage-matrix.json"),
        ("cases",),
    ),
    (
        "case-review",
        "aa-case-reviewer",
        _REVIEWER,
        ("review/case-review.json", "review/case-review-summary.md"),
        (),
    ),
    ("explore", "aa-explore", _EXPLORER, ("explore/exploration.json",), ()),
    ("intake", "aa-intake", _DOC_AUTHOR, (".qa.yaml", "requirement.md"), ()),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        base: _job(base, skill_id, agent_profile, outputs, extra_claims)
        for base, skill_id, agent_profile, outputs, extra_claims in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, _profile, outputs, _extra in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
