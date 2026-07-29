"""C4 试点：两个 skill 收敛为五节 manifest，且被删禁令已有运行时归宿。"""

import re

from assurance_agent import resources

PILOT = ("aa-api-plan", "aa-api-plan-reviewer")
SECTIONS = ("## Purpose", "## Inputs", "## Outputs", "## Boundaries", "## Domain Notes")
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


def test_pilot_skills_use_exactly_the_five_section_manifest_structure() -> None:
    for name in PILOT:
        text = resources.read_text("skills", name, "SKILL.md")
        headings = tuple(line for line in text.splitlines() if line.startswith("## "))
        assert headings == SECTIONS, name


def test_pilot_skills_drop_procedural_step_numbering() -> None:
    step = re.compile(r"^#{2,4}\s*(Step\s*\d|第\s*\d+\s*步)", re.MULTILINE)
    for name in PILOT:
        assert step.search(resources.read_text("skills", name, "SKILL.md")) is None, name


def test_reviewer_shrank_below_the_baseline() -> None:
    """reviewer 的篇幅主体是规则引擎散文，规则下沉后应大幅缩水。"""
    after = len(resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md").splitlines())
    assert after < 718 * 0.6, f"{after} lines"


def test_api_plan_shrank_only_modestly_because_format_contracts_stay() -> None:
    """Markdown 产物未注册，contract render 覆盖不到，格式契约必须留在 skill 里。"""
    after = len(resources.read_text("skills", "aa-api-plan", "SKILL.md").splitlines())
    assert after < 505, f"{after} lines"


def test_mechanised_prohibitions_are_gone_from_the_reviewer_prose() -> None:
    """这些规则已由 plan checks 和 capabilities gate 强制。"""
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    assert "alternate hidden-directory path" not in text
    assert "never narrow, drop, or reinterpret" not in text


def test_api_plan_retains_each_markdown_table_contract() -> None:
    """Markdown 未注册；每张表的语义与精确列顺序都是 authoring interface。"""
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    outputs = text[text.index("## Outputs") : text.index("## Boundaries")]
    for table, columns in PLAN_TABLES.items():
        assert f"{table} uses `{columns}`" in outputs, table
    assert "test_<case_id_lowercase>__<desc>" in outputs


def test_api_plan_case_id_column_requires_full_ids() -> None:
    """assert_ideal 零匹配判定按 Case ID 列精确相等，短号表不能替代全 ID。"""
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    assert "full case_id" in text.lower()


def test_domain_notes_retain_factory_adapter_and_live_server_knowledge() -> None:
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    notes = text[text.index("## Domain Notes") :]
    assert "factory" in notes.lower()
    assert "adapter" in notes.lower()
    assert "live-server" in notes.lower()


def test_reviewer_consumes_plan_check_document_as_facts_without_policy_adjudication() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    inputs = text[text.index("## Inputs") : text.index("## Outputs")]
    assert "PlanCheckDocument" in inputs
    assert "facts" in inputs
    assert "do not infer or apply a policy action" in inputs.lower()
    assert "downstream gate" in inputs.lower()
    assert "according to their runtime policy action" not in inputs


def test_reviewer_restores_every_gate_consumed_field() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    outputs = text[text.index("## Outputs") : text.index("## Boundaries")]
    for field in (
        "codegen_readiness",
        "auto_fix_allowed",
        "human_review_required",
        "risk_level",
    ):
        assert f"`{field}`" in outputs, field
    for mapping in (
        "`pass` | `ready` or `ready_with_warnings` | `false` | `false`",
        "`needs_fix` | `not_ready` | `true` | `false`",
        "`needs_human_review` | `not_ready` | `false` | `true`",
        "`reject` | `not_ready` | `false` | `true`",
    ):
        assert mapping in outputs


def test_reviewer_reports_risk_fact_without_hard_coding_policy_routing() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    outputs = text[text.index("## Outputs") : text.index("## Boundaries")]
    risk_line = next(line for line in outputs.splitlines() if "`risk_level` is a fact" in line)
    assert "`policy.human_review_risk_levels`" in risk_line
    assert "downstream gate" in risk_line.lower()
    for level in ("low", "medium", "high", "critical"):
        assert f"`{level}`" in risk_line

    # Risk levels may be reported only in the factual declaration. Any occurrence
    # elsewhere can recreate direct or inverse decision routing by severity tier.
    without_fact = text.replace(risk_line, "")
    assert re.search(r"\b(low|medium|high|critical)\b", without_fact, re.IGNORECASE) is None

    outputs_routing = outputs[
        outputs.index("Decision consistency is policy-neutral") : outputs.index("The Markdown summary")
    ]
    domain = text[text.index("## Domain Notes") :]
    domain_routing = domain[
        domain.index("Choose the semantic disposition") : domain.index("Findings describe")
    ]
    routing_terms = re.compile(
        r"\b(risk(?:_level|s)?|severity|tier|low|medium|high|critical|minor|major)\b",
        re.IGNORECASE,
    )
    assert routing_terms.search(outputs_routing) is None
    assert routing_terms.search(domain_routing) is None


def test_reviewer_never_writes_or_reports_a_workflow_state_delta() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    outputs = text[text.index("## Outputs") : text.index("## Boundaries")]
    boundaries = text[text.index("## Boundaries") : text.index("## Domain Notes")]
    assert "graph coordinator" in outputs.lower()
    assert "ledger" in outputs.lower()
    assert "no `workflow-state.yaml` state delta" in outputs
    assert "Do not write or propose updates to `workflow-state.yaml`" in boundaries
    assert "inline mode apply" not in text.lower()


def test_reviewer_retains_unmechanised_test_function_name_verification() -> None:
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    notes = text[text.index("## Domain Notes") :]
    assert "Test Function Mapping" in notes
    assert "test_<case_id_lowercase>__<desc>" in notes


def test_migration_completeness_keeps_reviewer_semantics_and_moves_only_mechanised_rules() -> None:
    from assurance_agent.verification.checks.registry import PLAN_CHECKS

    reviewer = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    for responsibility in (
        "assertion traceability",
        "coverage gap",
        "required capabilities",
        "Test Function Mapping",
    ):
        assert responsibility in reviewer, responsibility
    for mechanised in (
        "alternate hidden-directory path",
        "never narrow, drop, or reinterpret",
    ):
        assert mechanised not in reviewer, mechanised
    assert "check_shared_factory" in {check.__name__ for check in PLAN_CHECKS}
    stale_shared_factory_rule = re.compile(
        r"(?:existing|declared)\s+shared\s+(?:factor(?:y|ies)|capabilit(?:y|ies)).{0,80}"
        r"\b(?:reuse|reused|rewrite|rewriting)\b",
        re.IGNORECASE,
    )
    assert stale_shared_factory_rule.search(reviewer) is None
