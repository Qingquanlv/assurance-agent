"""Resident content checks for migrated skills + opencode assets (spec 8/9/4a)."""

import re
from pathlib import Path

import yaml

from assurance_agent import resources
from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ExecutionManifest,
    FactBaselineFull,
    FactBaselineUnavailable,
    FailureAnalysis,
    FixProposal,
    QaYaml,
    QualityGateResult,
    QualityReport,
    Review,
    SafetyCheck,
    WorkflowState,
)
from pydantic import BaseModel

RESIDUE_RE = re.compile(r"(?i)(?<![a-z])aws")
AWS_RESIDUE_ALLOWLIST: set[str] = set()
AA_REF_RE = re.compile(r"\b(aa-[a-z0-9-]+|writing-skills)\b")
AA_REF_ALLOWLIST = {"aa-full", "aa-api", "aa-e2e"}  # schema / memory path prefixes, not skills
FILE_SUFFIXES = {"json", "ts", "yaml", "yml", "md", "schema", "py"}
ALLOWLIST_FILE = Path(__file__).resolve().parents[1] / "data" / "skill_field_allowlist.txt"


def _walk_resource_files(*rel: str):
    """Yield (relpath_tuple) for every file under _resources/<rel> (recursive)."""
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)  # dir -> recurse
        except (NotADirectoryError, ValueError):
            yield child
        else:
            yield from _walk_resource_files(*child)


def _all_text_files(*rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for parts in _walk_resource_files(*rel):
        name = parts[-1]
        if name.endswith((".md", ".sh", ".mjs", ".ts", ".html", ".txt", ".yaml", ".yml", ".json", ".dot")):
            out["/".join(parts)] = resources.read_text(*parts)
    return out


def _skill_names() -> set[str]:
    return set(resources.iter_children("skills"))


def _agent_names() -> set[str]:
    return {n[:-3] for n in resources.iter_children("opencode", "agents") if n.endswith(".md")}


def _model_fields(*models: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for model in models:
        for field_name, field in model.model_fields.items():
            out.add(field_name)
            if field.alias:
                out.add(field.alias)
    return out


ARTIFACT_ROOTS: dict[str, set[str]] = {
    "review": _model_fields(Review),
    "fix_proposal": _model_fields(FixProposal),
    "failure_analysis": _model_fields(FailureAnalysis),
    "execution_manifest": _model_fields(ExecutionManifest),
    "quality_gate_result": _model_fields(QualityGateResult),
    "quality_report": _model_fields(QualityReport),
    "advisory": _model_fields(Advisory),
    "fact_baseline": _model_fields(FactBaselineFull, FactBaselineUnavailable),
    "workflow_state": _model_fields(WorkflowState),
    "safety_check": _model_fields(SafetyCheck),
    "apply_summary": _model_fields(ApplySummary),
    "case_yaml": _model_fields(CaseYaml),
    "qa_yaml": _model_fields(QaYaml),
}


def _load_field_allowlist() -> set[str]:
    entries: set[str] = set()
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def test_thirty_eight_skills_present() -> None:
    names = _skill_names()
    # Retro v3 analyzers, Improvement reviewer, and coverage-repair.
    assert len(names) == 36
    assert "writing-skills" in names
    assert "aa-workflow" in names
    assert "aa-dashboard" in names
    assert "aa-issue-analyzer" in names
    assert "aa-issue-triage-advisor" in names
    assert "aa-retro-issue-analysis" in names
    assert "aa-retro-workflow-analysis" in names
    assert "aa-retro-eval-analysis" in names
    assert "aa-coverage-repair" in names
    assert "aa-api-plan-fixer" not in names
    assert "aa-e2e-plan-fixer" not in names
    assert "aa-case-fixer" not in names
    assert not any(n.startswith("aws-") for n in names)


def test_improvement_reviewer_reads_multiline_agent_subject_projection() -> None:
    text = resources.read_text("skills", "aa-improvement-reviewer", "SKILL.md")

    assert "review-subjects/agent/${params.subject_sha256}.json" in text
    assert "Do not read the compact canonical subject" in text
    assert "Omit" in text
    for field in (
        "review_id",
        "improvement_id",
        "expected_improvement_version",
        "subject_sha256",
    ):
        assert field in text
    assert "runtime inserts" in text


def test_no_aws_residue_in_skills_and_opencode() -> None:
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode")}
    for relpath, text in files.items():
        for match in RESIDUE_RE.finditer(text):
            token = text[match.start() : match.start() + 3]
            if token not in AWS_RESIDUE_ALLOWLIST:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{relpath}:{line}:{token}")
    assert offenders == [], f"aws residue found: {offenders[:20]}"


def test_mechanised_plan_rules_live_in_the_runtime_not_the_skill_prose() -> None:
    """L1 路径、shared factory 与 assert_ideal 已从散文迁到确定性 check。"""
    from assurance_agent.verification.checks.registry import PLAN_CHECKS

    assert {check.__name__ for check in PLAN_CHECKS} >= {
        "check_l1_path",
        "check_shared_factory",
        "check_assert_ideal",
    }


def test_cross_skill_references_resolve() -> None:
    valid = _skill_names() | _agent_names() | AA_REF_ALLOWLIST
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode", "agents")}
    for relpath, text in files.items():
        for match in AA_REF_RE.finditer(text):
            token = match.group(1).rstrip(".")
            if token.endswith(".md"):
                token = token[:-3]
            if token not in valid:
                offenders.append(f"{relpath}: {token}")
    assert offenders == [], f"dangling aa-* references: {sorted(set(offenders))[:20]}"


def test_skill_field_references_subset_of_models() -> None:
    allowlist = _load_field_allowlist()
    offenders: list[str] = []
    for relpath, text in _all_text_files("skills").items():
        for root, fields in ARTIFACT_ROOTS.items():
            for match in re.finditer(rf"(?<![\w.]){root}\.([a-z_][a-z0-9_]*)", text):
                first = match.group(1)
                if first in FILE_SUFFIXES:
                    continue
                if first in fields:
                    continue
                if f"{root}.{first}" in allowlist:
                    continue
                offenders.append(f"{relpath}: {root}.{first}")
    assert offenders == [], f"skill fields not in M2 models: {sorted(set(offenders))[:20]}"


def test_agents_preserve_permission_floor() -> None:
    """No runtime agent grants an allow-rule for aa gate/status or workflow-state writes (spec 9).

    Agents legitimately MENTION `aa gate check` / workflow-state.json in prose (the
    prohibition text), so we assert on permission *allow-rules* only, not substrings.
    """
    allow_gate = re.compile(r'"[^"]*aa (gate|status)[^"]*"\s*:\s*allow')
    allow_state = re.compile(r'"[^"]*workflow-state\.yaml"\s*:\s*allow')
    agents = [n for n in resources.iter_children("opencode", "agents") if n.endswith(".md")]
    assert len(agents) == 7
    for name in agents:
        text = resources.read_text("opencode", "agents", name)
        assert allow_gate.search(text) is None, name
        assert allow_state.search(text) is None, name


def test_explore_permissions_are_removed_from_doc_author_and_owned_by_explorer() -> None:
    allow_risk = re.compile(r'"aa risk \*"\s*:\s*allow')
    allow_explore = re.compile(r'"\*\*qa/changes/\*\*/explore/\*\*"\s*:\s*allow')
    deny_context_edit = re.compile(r'"\*\*qa/changes/\*\*/explore/context\.json"\s*:\s*deny')
    explorer = resources.read_text("opencode", "agents", "aa-explorer.md")
    doc_author = resources.read_text("opencode", "agents", "aa-doc-author.md")

    assert allow_risk.search(explorer) is not None
    assert allow_explore.search(explorer) is not None
    assert deny_context_edit.search(explorer) is not None
    assert allow_risk.search(doc_author) is None
    assert allow_explore.search(doc_author) is None


def test_bounded_agents_disable_sandbox_escape_plugin_tools() -> None:
    for name in resources.iter_children("opencode", "agents"):
        if not name.endswith(".md"):
            continue
        text = resources.read_text("opencode", "agents", name)
        match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
        assert match is not None, name
        frontmatter = yaml.safe_load(match.group(1))
        expected = {
            "task": False,
            "task_create": False,
            "task_get": False,
            "task_list": False,
            "task_update": False,
            "call_omo_agent": False,
            "look_at": False,
            "skill_mcp": False,
            "interactive_bash": False,
            "monitor_start": False,
            "session_list": False,
            "session_read": False,
            "session_search": False,
            "session_info": False,
            "background_output": False,
            "background_cancel": False,
            "write": True,
            "artifact_write": True,
            "apply_patch": False,
            "webfetch": False,
            "websearch": False,
            "websearch_web_search_exa": False,
        }
        if name != "aa-intake-host.md":
            expected["workflow_start"] = False
        assert frontmatter.get("tools") == expected, name


def test_case_reviewer_requires_independent_product_source_verification() -> None:
    skill = resources.read_text("skills", "aa-case-reviewer", "SKILL.md")
    agent = resources.read_text("opencode", "agents", "aa-reviewer.md")

    assert "source_verification" in skill
    assert "independent" in skill
    assert "product source" in skill
    assert "exact projection of the frozen matrix" in skill
    assert "runtime recomputes this projection" in skill
    assert "source_verification" in agent
    assert "independently read" in agent


def test_case_design_requires_its_own_product_source_verification() -> None:
    skill = resources.read_text("skills", "aa-case-design", "SKILL.md")

    assert "## Product Source Verification" in skill
    assert "independently_read: true" in skill
    assert "reviewed_source_files" in skill
    assert "Explore source evidence is not a substitute" in skill


def test_case_design_requires_performance_execution_identity() -> None:
    skill = resources.read_text("skills", "aa-case-design", "SKILL.md")

    assert "automation.performance.scenario.capability" in skill
    assert "automation.performance.scenario.endpoint" in skill


def test_case_design_requires_selected_layer_automation_closure() -> None:
    skill = resources.read_text("skills", "aa-case-design", "SKILL.md")

    assert "Selected-layer automation closure" in skill
    assert "at least one `added` or `modified` case" in skill
    assert "same exact `type`" in skill
    assert "`automation.required: true`" in skill


def test_case_skills_define_semantic_endpoint_identifiers() -> None:
    design = resources.read_text("skills", "aa-case-design", "SKILL.md")
    reviewer = resources.read_text("skills", "aa-case-reviewer", "SKILL.md")

    for skill in (design, reviewer):
        assert "semantic endpoint identifier" in skill
        assert "literal HTTP route" in skill
        assert "must remain non-empty" in skill


def test_reviewer_agent_pins_authoring_enums_and_locator_keys() -> None:
    agent = resources.read_text("opencode", "agents", "aa-reviewer.md")

    assert "`pass`, never the read-only compatibility value `approved`" in agent
    assert "`low`, `medium`, `high`, `critical`, or `blocking`" in agent
    assert "`artifact`, `case_id`, and `key`" in agent
    assert "Never emit `lines`, `line`, `field`" in agent


def test_codegen_skills_explain_strict_manifest_role_and_reuse_rules() -> None:
    for layer in ("api", "e2e", "fuzz", "performance"):
        skill = resources.read_text("skills", f"aa-{layer}-codegen", "SKILL.md")
        assert "case_ids: []" in skill
        assert "selected private-root `test_entry`" in skill
        assert "unchanged support dependencies" in skill


def test_fuzz_codegen_uses_live_schema_mode_without_inventing_app_modules() -> None:
    skill = resources.read_text("skills", "aa-fuzz-codegen", "SKILL.md")

    assert "QA_FUZZ_SCHEMA_MODE" in skill
    assert "live SUT" in skill
    assert "never import an application module" in skill
    assert "never invent" in skill
    normalized = " ".join(skill.split())
    assert "generation input, not as an assertion" in normalized
    assert "exact `(METHOD, PATH)` membership" in normalized


def test_e2e_codegen_requires_dom_verified_login_locators() -> None:
    skill = resources.read_text("skills", "aa-e2e-codegen", "SKILL.md")

    assert "actual login DOM" in skill
    assert "accessible name" in skill
    assert "placeholder" in skill
    assert "do not call `asyncio.run()`" in skill


def test_http_codegen_and_review_skills_require_runtime_contract_closure() -> None:
    for name in (
        "aa-api-codegen",
        "aa-api-plan-reviewer",
        "aa-e2e-codegen",
        "aa-e2e-plan-reviewer",
        "aa-fuzz-codegen",
        "aa-fuzz-plan-reviewer",
        "aa-performance-codegen",
        "aa-performance-plan-reviewer",
    ):
        skill = resources.read_text("skills", name, "SKILL.md")
        assert "OpenAPI" in skill, name
        assert "method" in skill.lower(), name
        assert "path" in skill.lower(), name

    api = resources.read_text("skills", "aa-api-codegen", "SKILL.md")
    performance = resources.read_text("skills", "aa-performance-codegen", "SKILL.md")
    assert "never\n  assume `data.id` exists" in api
    assert "do not require a create\nresponse to return an identifier" in performance


def test_http_plan_reviewers_treat_repository_and_openapi_as_review_evidence() -> None:
    for name in (
        "aa-api-plan-reviewer",
        "aa-e2e-plan-reviewer",
        "aa-fuzz-plan-reviewer",
        "aa-performance-plan-reviewer",
    ):
        skill = resources.read_text("skills", name, "SKILL.md")
        normalized = " ".join(skill.split())
        assert "review input list is not an evidence boundary" in normalized, name
        assert "must perform the read-only inspection yourself" in normalized, name
        assert "plan text does not embed the proof" in normalized, name


def test_fuzz_and_performance_skills_preserve_observed_semantics() -> None:
    for name in ("aa-fuzz-plan", "aa-fuzz-plan-reviewer", "aa-fuzz-codegen"):
        skill = " ".join(resources.read_text("skills", name, "SKILL.md").split()).lower()
        assert "unconstrained long string" in skill, name
        assert "schema" in skill, name

    for name in (
        "aa-performance-plan",
        "aa-performance-plan-reviewer",
        "aa-performance-codegen",
    ):
        skill = " ".join(resources.read_text("skills", name, "SKILL.md").split()).lower()
        assert "pagination" in skill, name
        assert "unwrap" in skill, name


def test_fuzz_and_performance_skills_validate_positive_seed_data() -> None:
    for name in (
        "aa-fuzz-plan",
        "aa-fuzz-plan-reviewer",
        "aa-fuzz-codegen",
        "aa-performance-plan",
        "aa-performance-plan-reviewer",
        "aa-performance-codegen",
    ):
        skill = resources.read_text("skills", name, "SKILL.md")
        normalized = " ".join(skill.split()).lower()
        assert "positive seed" in normalized, name
        assert "example.test" in normalized, name


def test_case_design_forbids_inventing_mrc_keys_without_knowledge_proposal() -> None:
    """Verification Metrics M1 Task 9: MRC keys are a closed set."""
    design = resources.read_text("skills", "aa-case-design", "SKILL.md")
    reviewer = resources.read_text("skills", "aa-case-reviewer", "SKILL.md")

    assert "MRC closed-key discipline" in design
    assert "discovered_candidates" in design
    assert "Do **not** freely invent MRC keys" in design or "do not freely invent" in design.lower()
    assert "knowledge proposal" in design.lower()
    assert "discovered_candidates" in reviewer
    assert "invent MRC keys" in reviewer or "freely invented" in reviewer


def test_aa_retro_skill_is_current_run_candidate_boundary() -> None:
    """aa-retro must emit schema-v2 candidates from current-run context only."""
    text = resources.read_text("skills", "aa-retro", "SKILL.md")
    assert "proposal-candidates.json" in text
    assert "schema_version" in text
    assert "context_sha256" in text
    assert "signal_ids" in text
    assert "Read only" in text or "Required (only)" in text
    for kind in (
        "prompt_improvement",
        "fixture_improvement",
        "test_improvement",
        "workflow_improvement",
        "domain_knowledge",
    ):
        assert kind in text
    for delivery in ("memory_patch", "change_draft", "knowledge_delta"):
        assert delivery in text
    # Legacy proposal vocabulary and broad historical reads are forbidden.
    assert "proposals.json" not in text
    assert "finding_kind" not in text
    assert "apply_kind" not in text
    assert "workflow_bug" not in text
    assert "issue_export" not in text
    assert "promotions.json" not in text
    assert "qa/issues/**" not in text and "qa/issues/" not in text
    assert "qa/archive" not in text
    assert "schemas/workflow-schema.yaml" not in text
    assert "problems.json" not in text
    assert "events.jsonl" not in text
    assert "Optional read-only context" not in text


def test_issue_analyzer_reconciles_review_findings_with_post_codegen_evidence() -> None:
    text = resources.read_text("skills", "aa-issue-analyzer", "SKILL.md")

    assert "point-in-time advisory" in text
    assert "post-Codegen evidence" in text
    assert "Do not propose a candidate" in text


def test_retro_issue_analysis_keeps_product_defects_out_of_improvements() -> None:
    text = resources.read_text("skills", "aa-retro-issue-analysis", "SKILL.md")

    assert "surface.kind == workflow" in text
    assert "product API, schema, validation" in text
    assert "Never weaken an assert_ideal contract" in text
    assert "omit the signal" in text


def test_retro_analyzers_read_multiline_agent_slice_companions() -> None:
    for domain in ("issue", "workflow", "eval"):
        text = resources.read_text("skills", f"aa-retro-{domain}-analysis", "SKILL.md")

        assert f"evidence/agent/{domain}-slice.json" in text
        assert f"Read only `qa/retro/<retro-id>/evidence/{domain}-slice.json`" not in text
        assert "canonical slice" in text
