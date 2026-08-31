from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families

WORKFLOW_MODULE_ID = "assurance.generation.workflow"
WORKFLOW_RESOURCE_ID = "assurance.generation.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("generate",)
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

_DOC_AUTHOR = "assurance-v1-doc-author"
_REVIEWER = "assurance-v1-reviewer"
_TEST_AUTHOR = "assurance-v1-test-author"

_PLAN_FILES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "api": (
            "plans/api-plan.md",
            "plans/api-test-data-plan.md",
            "plans/api-codegen-plan.md",
            "plans/api-codegen-mapping.json",
            "plans/m3-review-summary.md",
        ),
        "e2e": (
            "plans/e2e-plan.md",
            "plans/e2e-test-data-plan.md",
            "plans/e2e-codegen-plan.md",
            "plans/e2e-codegen-mapping.json",
            "plans/m4-review-summary.md",
        ),
        "fuzz": (
            "plans/fuzz-plan.md",
            "plans/fuzz-codegen-plan.md",
            "plans/fuzz-codegen-mapping.json",
            "plans/fuzz-review-summary.md",
        ),
        "performance": (
            "plans/performance-plan.md",
            "plans/performance-codegen-plan.md",
            "plans/performance-codegen-mapping.json",
            "plans/performance-review-summary.md",
        ),
    }
)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _review_outputs(family: str) -> tuple[str, ...]:
    return (f"review/{family}-plan-review.json", f"review/{family}-plan-review-summary.md")


def _codegen_outputs(family: str, *, fix: bool = False) -> tuple[str, ...]:
    suffix = "-fix" if fix else ""
    return (
        f"codegen/{family}-codegen{suffix}-summary.md",
        f"codegen/{family}-generated-files.json",
    )


def _job(base: str, skill_id: str, agent_profile: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    family, _, stage = base.partition(".")
    claim_outputs = outputs
    if stage in {"codegen", "codegen-fix"}:
        claim_outputs = (*outputs, f"generated/{family}/files")
    return AgentExecutionContract(
        contract_id=f"assurance.generation.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*claim_outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("api.codegen-fix", "aa-api-codegen-fixer", _TEST_AUTHOR, _codegen_outputs("api", fix=True)),
    ("api.codegen", "aa-api-codegen", _TEST_AUTHOR, _codegen_outputs("api")),
    ("api.plan-review", "aa-api-plan-reviewer", _REVIEWER, _review_outputs("api")),
    ("api.plan", "aa-api-plan", _DOC_AUTHOR, _PLAN_FILES["api"]),
    ("e2e.codegen-fix", "aa-e2e-codegen-fixer", _TEST_AUTHOR, _codegen_outputs("e2e", fix=True)),
    ("e2e.codegen", "aa-e2e-codegen", _TEST_AUTHOR, _codegen_outputs("e2e")),
    ("e2e.plan-review", "aa-e2e-plan-reviewer", _REVIEWER, _review_outputs("e2e")),
    ("e2e.plan", "aa-e2e-plan", _DOC_AUTHOR, _PLAN_FILES["e2e"]),
    ("fuzz.codegen", "aa-fuzz-codegen", _TEST_AUTHOR, _codegen_outputs("fuzz")),
    ("fuzz.plan-review", "aa-fuzz-plan-reviewer", _REVIEWER, _review_outputs("fuzz")),
    ("fuzz.plan", "aa-fuzz-plan", _DOC_AUTHOR, _PLAN_FILES["fuzz"]),
    ("performance.codegen", "aa-performance-codegen", _TEST_AUTHOR, _codegen_outputs("performance")),
    ("performance.plan-review", "aa-performance-plan-reviewer", _REVIEWER, _review_outputs("performance")),
    ("performance.plan", "aa-performance-plan", _DOC_AUTHOR, _PLAN_FILES["performance"]),
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
    "GENERATION_FAMILIES",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
    "validate_selected_families",
]
