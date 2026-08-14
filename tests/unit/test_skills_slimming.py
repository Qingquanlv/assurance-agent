"""C4 pilot skills retain their stable authoring interfaces after slimming."""

import re

import pytest

from assurance_agent import resources
from assurance_agent.verification.checks.registry import PLAN_CHECKS


PILOT = ("aa-api-plan", "aa-api-plan-reviewer")
E2E_PILOT = ("aa-e2e-plan", "aa-e2e-plan-reviewer")
SECTIONS = ("## Purpose", "## Inputs", "## Outputs", "## State Authority", "## Boundaries", "## Domain Notes")
PLAN_TABLES = {
    "Scope": "Case ID | Title",
    "API Targets": "Case ID | Scenario | Method | Path | Expected",
    "Auth Strategy": "Case ID | Auth",
    "Request Strategy": "Case ID | Headers | Body | Params",
    "Assertion Strategy": "Case ID | Assertions",
    "Mock Strategy": "Case ID | Dependency | Approach",
    "Cleanup Strategy": "Case ID | Cleanup",
    "Output File Candidates": "Case ID | Target File",
    "Required Data": "Entity | State | Capability",
    "Capability Mapping": "Need | Capability | Source | Status (found/missing/warning)",
    "Factory / Boundary Strategy": "Entity | Ring | Preferred method | Notes",
    "No Data Required Cases": "Case ID | Rationale",
    "Target Files": "File | Purpose",
    "Test Function Mapping": "Case ID | Test Function | Target File",
    "Factory Mapping": "Entity | Shared Module | Function | Ownership | Required By",
    "Adapter Mapping": "Entity | API Adapter | Transport | Cleanup",
    "Fixture Mapping": "Fixture | Source Factory | Wrapper Only (yes/no) | Required By",
    "Helper Mapping": "Helper | Purpose | Required By",
    "Import Strategy": "Target File | Imports",
    "Assertion Mapping": "Case ID | Assertions",
    "Data Setup Mapping": "Case ID | Setup | Capability",
    "Cleanup Mapping": "Case ID | Cleanup | Capability",
    "Run Guidance": "Target | Pytest Args | Markers | Environment",
}
E2E_PLAN_TABLES = {
    "Scope": "Case ID | Title",
    "Required Data": "Entity | State | Capability",
    "Capability Mapping": "Need | Capability | Source | Status (found/missing/warning)",
    "Target Files": "File | Purpose",
    "Test Function Mapping": "Case ID | Test Function | Target File",
    "Factory Mapping": "Entity | Shared Module | Function | Ownership | Required By",
    "Adapter Mapping": "Entity | E2E Adapter | Transport | Cleanup",
    "Fixture Mapping": "Fixture | Source Factory | Wrapper Only (yes/no) | Required By",
    "Assertion Mapping": "Case ID | Assertions",
    "Cleanup Mapping": "Case ID | Cleanup | Capability",
    "Run Guidance": "Target | Pytest Args | Markers | Environment",
}


def _section(text: str, start: str, end: str | None = None) -> str:
    offset = text.index(start)
    return text[offset:] if end is None else text[offset : text.index(end, offset)]


def _assert_risk_routing_is_policy_neutral(text: str) -> None:
    forbidden = re.compile(
        r"(?:risk|severity|high|critical).{0,50}"
        r"(?:routes?|requires?|forces?|blocks?|sets?).{0,30}"
        r"(?:human review|decision|codegen readiness|next_action|stop)",
        re.IGNORECASE,
    )
    assert forbidden.search(text) is None


def _assert_no_shared_factory_policy(text: str) -> None:
    forbidden = re.compile(
        r"(?:must|should|never|always|do not).{0,40}"
        r"(?:reuse|rewrite|duplicate|create).{0,40}(?:shared )?factor",
        re.IGNORECASE,
    )
    assert forbidden.search(text) is None


def test_pilot_skills_expose_only_the_five_manifest_sections() -> None:
    for name in PILOT:
        text = resources.read_text("skills", name, "SKILL.md")
        headings = tuple(line for line in text.splitlines() if line.startswith("## "))
        assert headings == SECTIONS, name
        assert re.search(r"^#{2,4}\s*(?:Step\s*\d|第\s*\d+\s*步)", text, re.MULTILINE) is None


def test_pilot_skills_stay_below_the_task_9_line_baselines() -> None:
    reviewer_lines = len(resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md").splitlines())
    api_plan_lines = len(resources.read_text("skills", "aa-api-plan", "SKILL.md").splitlines())

    assert reviewer_lines < 718 * 0.6, reviewer_lines
    assert api_plan_lines < 505, api_plan_lines


def test_api_plan_retains_the_markdown_authoring_contract() -> None:
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    domain = _section(text, "## Domain Notes")

    for table, columns in PLAN_TABLES.items():
        assert f"{table} uses `{columns}`" in domain, table
    assert "test_<case_id_lowercase>__<desc>" in domain
    assert "full case_id" in text.lower()


def test_api_plan_ownership_matches_shared_factory_runtime_policy() -> None:
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    domain = _section(text, "## Domain Notes")

    assert "`reuse` for every symbol already declared by L1 knowledge" in domain
    assert "`create-if-missing` only when L1 does not declare that shared symbol" in domain
    assert "check_shared_factory" in {check.__name__ for check in PLAN_CHECKS}


def test_reviewer_consumes_mechanical_checks_as_facts_not_policy() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    purpose = _section(text, "## Purpose", "## Inputs")

    assert "PlanCheckDocument" in purpose
    assert "facts" in purpose
    assert "do not infer or apply a policy action" in purpose.lower()
    assert "downstream gate" in purpose.lower()


def test_reviewer_declares_its_gate_routing_contract() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    outputs = _section(text, "## Domain Notes")

    assert "schema contract is supplied by the runtime" in outputs
    for field in (
        "codegen_readiness",
        "auto_fix_allowed",
        "human_review_required",
        "risk_level",
    ):
        assert f"`{field}`" in outputs, field
    assert "`reject` | `not_ready` | `false` | `true` | `stop`" in outputs
    assert "policy.human_review_risk_levels" in outputs


def test_reviewer_keeps_only_unmechanised_semantic_review_responsibilities() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")

    for responsibility in (
        "assertion traceability",
        "coverage gap",
        "required capabilities",
        "Test Function Mapping",
    ):
        assert responsibility in text, responsibility
    assert "alternate hidden-directory path" not in text
    assert "never narrow, drop, or reinterpret" not in text


def test_reviewer_canonical_prose_passes_both_policy_mutation_guards() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")

    _assert_risk_routing_is_policy_neutral(text)
    _assert_no_shared_factory_policy(text)


@pytest.mark.parametrize(
    "rule",
    [
        "High risk routes to human review.",
        "Critical severity blocks codegen readiness.",
        "Risk sets next_action to stop.",
    ],
)
def test_risk_routing_mutation_guard_rejects_policy_leaking_back_into_the_skill(rule: str) -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    mutated = text.replace("## Domain Notes", f"## Domain Notes\n\n{rule}", 1)

    with pytest.raises(AssertionError):
        _assert_risk_routing_is_policy_neutral(mutated)


@pytest.mark.parametrize(
    "rule",
    [
        "Always reuse a shared factory.",
        "Do not duplicate shared factories.",
        "You must not create a local shared factory.",
    ],
)
def test_shared_factory_mutation_guard_rejects_mechanical_policy_in_reviewer(rule: str) -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    mutated = text.replace("## Domain Notes", f"## Domain Notes\n\n{rule}", 1)

    with pytest.raises(AssertionError):
        _assert_no_shared_factory_policy(mutated)


def test_reviewer_does_not_own_workflow_state() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    state = _section(text, "## State Authority", "## Boundaries")
    domain = _section(text, "## Domain Notes")

    assert "`owner: graph_ledger`" in state
    assert "`agent_state_writes: forbidden`" in state
    assert "graph coordinator" in domain.lower() or "ledger" in domain.lower()


def test_e2e_plan_retains_the_markdown_authoring_contract() -> None:
    text = resources.read_text("skills", "aa-e2e-plan", "SKILL.md")
    contract = _section(text, "## Domain Notes")

    for table, columns in E2E_PLAN_TABLES.items():
        assert f"{table} uses `{columns}`" in contract, table
    assert "test_<case_id_lowercase>__<desc>" in contract
    assert "full case_id" in text.lower()


def test_e2e_plan_ownership_matches_shared_factory_runtime_policy() -> None:
    text = resources.read_text("skills", "aa-e2e-plan", "SKILL.md")
    contract = _section(text, "## Domain Notes")

    assert "`reuse` for every symbol already declared by L1 knowledge" in contract
    assert "`create-if-missing` only when L1 does not declare that shared symbol" in contract
    assert "check_shared_factory" in {check.__name__ for check in PLAN_CHECKS}


def test_e2e_reviewer_consumes_mechanical_checks_as_facts_not_policy() -> None:
    text = resources.read_text("skills", "aa-e2e-plan-reviewer", "SKILL.md")
    purpose = _section(text, "## Purpose", "## Inputs")

    assert "PlanCheckDocument" in purpose
    assert "review/e2e-plan-checks.json" in purpose
    assert "facts" in purpose.lower()
    assert "do not infer or apply a policy action" in purpose.lower()
    assert "downstream gate" in purpose.lower()


def test_e2e_reviewer_declares_its_gate_routing_contract() -> None:
    text = resources.read_text("skills", "aa-e2e-plan-reviewer", "SKILL.md")
    outputs = _section(text, "## Domain Notes")

    assert "schema contract is supplied by the runtime" in outputs
    for field in (
        "codegen_readiness",
        "auto_fix_allowed",
        "human_review_required",
        "risk_level",
    ):
        assert f"`{field}`" in outputs, field
    assert "`reject` | `not_ready` | `false` | `true` | `stop`" in outputs
    assert "policy.human_review_risk_levels" in outputs


def test_e2e_reviewer_keeps_semantic_factory_mapping_review() -> None:
    text = resources.read_text("skills", "aa-e2e-plan-reviewer", "SKILL.md")

    for responsibility in (
        "Factory Mapping",
        "Adapter Mapping",
        "assertion traceability",
        "required capabilities",
        "Test Function Mapping",
    ):
        assert responsibility in text, responsibility
    assert "never write" in text.lower() and "e2e-plan-checks.json" in text


def test_e2e_reviewer_canonical_prose_passes_both_policy_mutation_guards() -> None:
    text = resources.read_text("skills", "aa-e2e-plan-reviewer", "SKILL.md")

    _assert_risk_routing_is_policy_neutral(text)
    _assert_no_shared_factory_policy(text)


def test_e2e_fixer_repairs_factory_mapping_only_on_authorized_findings() -> None:
    text = resources.read_text("skills", "aa-e2e-plan-fixer", "SKILL.md")

    assert "Factory Mapping" in text
    assert "auto_fix_plan" in text
    assert "e2e-plan-checks.json" in text
    assert "Do not edit" in text
    assert ".aa/data-knowledge.yaml" in text
    assert "Never promote proposal content into L1" in text


def test_fuzz_plan_binds_codegen_plan_to_precommit_mapping_contract() -> None:
    planner = resources.read_text("skills", "aa-fuzz-plan", "SKILL.md")
    reviewer = resources.read_text("skills", "aa-fuzz-plan-reviewer", "SKILL.md")

    for text in (planner, reviewer):
        assert "fuzz-codegen-plan.md" in text
        assert "exact `## Test Function Mapping` heading" in text
        assert "Case ID | Test Function | Target File" in text
        assert "Schema Acquisition" in text
    assert "fails closed" in planner
    assert "Independently inspect both" in reviewer
