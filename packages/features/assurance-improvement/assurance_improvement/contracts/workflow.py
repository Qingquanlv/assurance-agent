from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

_ARCHIVER = "assurance-v1-archiver"
_DOC_AUTHOR = "assurance-v1-doc-author"
_REVIEWER = "assurance-v1-reviewer"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(base: str, skill_id: str, agent_profile: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.improvement.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("archive", "aa-archive", _ARCHIVER, ("archive/archive-receipt.json",)),
    ("improvement-review", "aa-improvement-reviewer", _REVIEWER, ("review/improvement-review.json",)),
    ("retro-eval-analysis", "aa-retro-eval-analysis", _DOC_AUTHOR, ("retro/retro-eval-analysis.json",)),
    ("retro-issue-analysis", "aa-retro-issue-analysis", _DOC_AUTHOR, ("retro/retro-issue-analysis.json",)),
    (
        "retro-workflow-analysis",
        "aa-retro-workflow-analysis",
        _DOC_AUTHOR,
        ("retro/retro-workflow-analysis.json",),
    ),
    ("retro", "aa-retro", _DOC_AUTHOR, ("retro/retro.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {base: _job(base, skill_id, agent_profile, outputs) for base, skill_id, agent_profile, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, _profile, outputs in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "OUTPUT_ROUTE_TEMPLATES",
]
