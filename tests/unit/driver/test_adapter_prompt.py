from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.driver.adapter import (
    Adapter,
    DriverError,
    PhaseRequest,
    PhaseResult,
)
from assurance_agent.workflow.driver.phase_prompt import build_phase_prompt
from assurance_agent.workflow.graph.agent_api import _elide_middle, build_node_prompt


def test_phase_request_defaults() -> None:
    req = PhaseRequest(change_id="CH-1", phase_id="explore", prompt="do it")
    assert req.skill is None
    assert req.agent is None
    assert req.prompt == "do it"


def test_phase_result_error_optional() -> None:
    ok = PhaseResult(ok=True, output="done")
    assert ok.error is None
    bad = PhaseResult(ok=False, output="", error="boom")
    assert bad.error == "boom"


def test_driver_error_is_aa_error() -> None:
    assert issubclass(DriverError, AaError)


def test_adapter_protocol_is_runtime_checkable() -> None:
    class Ok:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="")

    class NotAdapter:
        pass

    assert isinstance(Ok(), Adapter)
    assert not isinstance(NotAdapter(), Adapter)


def test_build_phase_prompt_binds_skill_and_change() -> None:
    prompt = build_phase_prompt("aa-explore", "explore", "CH-1")
    assert "skill(name='aa-explore')" in prompt
    assert "change_id='CH-1'" in prompt
    assert "qa/changes/CH-1/" in prompt
    assert "phase explore" in prompt
    assert "Do NOT modify workflow-state.yaml" in prompt


def test_build_phase_prompt_fix_proposal_binding_only_for_that_phase() -> None:
    fix = build_phase_prompt("aa-fix-proposal", "fix-proposal", "CH-1")
    assert "source_batch_id" in fix
    assert "source_analysis_sha256" in fix
    other = build_phase_prompt("aa-explore", "explore", "CH-1")
    assert "source_analysis_sha256" not in other


def test_build_phase_prompt_has_no_legacy_aws_reference() -> None:
    prompt = build_phase_prompt("aa-api-codegen", "api-codegen", "CH-1")
    assert "aws" not in prompt.lower()
    assert "aa gate/status" in prompt


def test_phase_prompt_binds_fanout_item() -> None:
    prompt = build_phase_prompt("aa-case-gen", "case-gen[menu]", "CH-1", item="menu")
    assert "item 'menu'" in prompt
    assert "case-gen[menu]" in prompt
    plain = build_phase_prompt("aa-explore", "explore", "CH-1")
    assert "fanned-out" not in plain


def test_build_phase_prompt_injects_skill_memory_from_project_root(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "aa-explore.md").write_text("- keep this rule\n", encoding="utf-8")
    prompt = build_phase_prompt("aa-explore", "explore", "CH-1", project_root=tmp_path)
    assert "Active skill memory for aa-explore" in prompt
    assert "keep this rule" in prompt


def test_build_node_prompt_injects_skill_memory_from_frozen_root(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "aa-api-codegen.md").write_text("- validate factories first\n", encoding="utf-8")
    prompt = build_node_prompt(
        "aa-api-codegen",
        "api-codegen",
        "CH-1",
        allowed_writes=["change:codegen/**"],
        memory_root=tmp_path,
    )
    assert "validate factories first" in prompt
    assert "Active skill memory for aa-api-codegen" in prompt


def test_build_node_prompt_injects_contract_violation_prior_failure() -> None:
    prompt = build_node_prompt(
        "aa-api-plan-reviewer",
        "review",
        "CH-1",
        allowed_writes=["change:review/**"],
        prior_failure="output 'change:review/api-plan-review.json' failed review schema validation: required_capabilities",
        prior_error_kind="invalid_output",
    )
    assert "PRIOR ATTEMPT FAILED (invalid_output)" in prompt
    assert "required_capabilities" in prompt
    assert "violated the artifact contract" in prompt


def test_build_node_prompt_contract_retry_rebuilds_from_committed_inputs() -> None:
    prompt = build_node_prompt(
        "aa-case-design",
        "case-design",
        "CH-1",
        allowed_writes=["change:cases/**", "change:proposal.md"],
        prior_failure="case-design Product Source Verification is invalid",
        prior_error_kind="invalid_output",
    )

    assert "failed attempt's workspace and artifacts were discarded" in prompt
    assert "rebuild every declared output from committed inputs" in prompt
    assert "Open the declared output you wrote" not in prompt
    assert "keep all other content unchanged" not in prompt


def test_build_node_prompt_truncates_long_prior_failure() -> None:
    long = "x" * 1200
    prompt = build_node_prompt(
        "aa-api-plan-reviewer",
        "review",
        "CH-1",
        allowed_writes=["change:review/**"],
        prior_failure=long,
        prior_error_kind="forbidden_write",
    )
    assert "PRIOR ATTEMPT FAILED (forbidden_write)" in prompt
    assert "…" in prompt
    assert ("x" * 1200) not in prompt


def test_elide_middle_honors_bound_when_only_one_content_character_fits() -> None:
    assert _elide_middle("abcdef", max_chars=4) == "a\n…\n"


def test_build_node_prompt_preserves_both_ends_of_long_prior_failure() -> None:
    registry_fields = (
        "case_id",
        "title",
        "level",
        "domain",
        "objective",
        "preconditions",
        "steps.0.action",
        "steps.0.expected",
        "oracle.type",
        "oracle.expected",
        "traceability.requirement_ids",
        "coverage.capability",
    )
    registry_detail = "\n".join(
        f"{field}\n  Field required [type=missing, input_value={{'broken': True}}, input_type=dict]"
        for field in registry_fields
    )
    prior_failure = (
        "multiple output contract violations:\n"
        "- output 'change:cases/system/api/case.yaml' failed case_yaml schema validation: "
        f"{len(registry_fields)} validation errors for CaseArtifact\n"
        f"{registry_detail}\n"
        "- case-design proposal.md must contain exactly one "
        "'## Product Source Verification' section\n"
        "Required format:\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `<project-relative product source path>`"
    )
    assert prior_failure.index("Required format:") > 800

    prompt = build_node_prompt(
        "aa-case-design",
        "case-design",
        "CH-1",
        allowed_writes=["change:cases/", "change:proposal.md"],
        prior_failure=prior_failure,
        prior_error_kind="invalid_output",
    )

    assert "change:cases/system/api/case.yaml" in prompt
    assert "case_yaml schema validation" in prompt
    assert "Required format:" in prompt
    assert "- independently_read: true" in prompt
    assert "reviewed_source_files:" in prompt
    detail_header = "PRIOR ATTEMPT FAILED (invalid_output) — do not repeat it:\n"
    detail_start = prompt.index(detail_header) + len(detail_header)
    detail_end = prompt.index(
        "\nYour previous attempt violated the artifact contract.",
        detail_start,
    )
    bounded_detail = prompt[detail_start:detail_end]
    assert len(bounded_detail) <= 800
    assert "…" in bounded_detail
    assert bounded_detail != prior_failure


def test_build_node_prompt_skips_transient_prior_failure() -> None:
    prompt = build_node_prompt(
        "aa-api-plan-reviewer",
        "review",
        "CH-1",
        allowed_writes=["change:review/**"],
        prior_failure="timed out waiting for model",
        prior_error_kind="timeout",
    )
    assert "PRIOR ATTEMPT FAILED" not in prompt


def test_plan_reviewer_prompt_frontloads_nonempty_required_capabilities_contract() -> None:
    for skill, output in (
        ("aa-api-plan-reviewer", "change:review/api-plan-review.json"),
        ("aa-e2e-plan-reviewer", "change:review/plan-review.json"),
    ):
        prompt = build_node_prompt(
            skill,
            "review",
            "CH-1",
            allowed_writes=["change:review/**"],
            outputs=[output],
        )

        assert "required_capabilities" in prompt
        assert "non-empty" in prompt
        assert "fully qualified C4 leaf keys" in prompt
        assert "capabilities.adapters" in prompt
        assert prompt.index("required_capabilities") > prompt.index("Produce only node")
    assert "timed out waiting for model" not in prompt


def test_case_design_prompt_expands_registered_case_contract_from_real_outputs() -> None:
    prompt = build_node_prompt(
        "aa-case-design",
        "case-design",
        "CH-1",
        allowed_writes=["change:.qa.yaml", "change:proposal.md", "change:cases/**"],
        outputs=["change:.qa.yaml", "change:proposal.md", "change:cases/"],
    )

    assert prompt.count("cases/**/case.yaml must be a CaseYamlAuthoring") == 1
    assert prompt.count("automation.performance.scenario.capability") == 1
    assert prompt.count("automation.performance.scenario.endpoint") == 1
    assert "proposal.md must be" not in prompt


def test_retro_analyzer_prompt_bounds_writes_by_contract_not_prose() -> None:
    """ANALYSIS ONLY 的强制点是 contract authorization_writes（forbidden_write），
    不是 prompt 散文；SKILL.md 里已有等价的领域边界说明。"""
    prompt = build_node_prompt(
        "aa-retro-issue-analysis",
        "analyze",
        "RETRO-RUN-1",
        allowed_writes=["project:qa/retro/retro-1/signals/domain.json"],
        outputs=["project:qa/retro/retro-1/signals/domain.json"],
    )

    assert "Authorized write paths: project:qa/retro/retro-1/signals/domain.json" in prompt
    assert "Produce only node analyze's declared outputs" in prompt


def test_retro_proposer_prompt_frontloads_v3_draft_contract() -> None:
    prompt = build_node_prompt(
        "aa-retro",
        "propose-improvements",
        "RETRO-RUN-1",
        allowed_writes=["project:qa/retro/retro-1/proposal-candidates.json"],
        outputs=["project:qa/retro/retro-1/proposal-candidates.json"],
    )

    assert "ImprovementCandidateDocumentDraftV3" in prompt
    assert "schema_version '3'" in prompt
    assert "omit context_sha256" in prompt
    assert "include signal_ids" in prompt
    assert "never emit legacy intent_key" in prompt


def test_prompt_has_no_contract_clause_for_unregistered_outputs() -> None:
    prompt = build_node_prompt(
        "aa-api-plan",
        "plan",
        "CH-1",
        allowed_writes=["change:plans/**"],
        outputs=["change:plans/api-plan.md"],
    )

    assert "OUTPUT CONTRACT" not in prompt
