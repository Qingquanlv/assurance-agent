"""OUTPUT CONTRACT 子句从 artifact registry 的 pydantic 模型渲染（spec C1）。"""

import pytest

from assurance_agent.verification.contract_render import render_output_contract


def test_unregistered_output_renders_nothing() -> None:
    assert render_output_contract(["change:plans/api-plan.md"]) == ""
    assert render_output_contract([]) == ""


def test_directory_output_is_skipped() -> None:
    assert render_output_contract(["project:qa/archive/CH-1/"]) == ""


def test_root_prefixes_are_stripped_before_registry_lookup() -> None:
    clause = render_output_contract(["change:review/api-plan-review.json"])
    assert "review/api-plan-review.json" in clause
    assert "change:" not in clause


def test_required_fields_and_literal_enums_come_from_the_model() -> None:
    clause = render_output_contract(["change:review/api-plan-review.json"])
    assert "required fields" in clause
    assert "schema_version" in clause
    assert "decision" in clause
    assert "needs_human_review" in clause


def test_plan_review_prompt_notes_are_rendered() -> None:
    clause = render_output_contract(["change:review/api-plan-review.json"])
    assert "required_capabilities" in clause
    assert "non-empty" in clause
    assert "fully qualified C4 leaf keys" in clause
    assert "capabilities.adapters" in clause


def test_case_review_prompt_names_nested_source_verification_fields() -> None:
    clause = render_output_contract(["change:review/case-review.json"])

    assert "source_verification.independent" in clause
    assert "source_verification.reviewed_source_files" in clause
    assert "source_verification.verified_claims" in clause
    assert "verified_claims[].claim" in clause
    assert "verified_claims[].evidence_files" in clause


def test_authoring_model_wins_over_validation_model() -> None:
    clause = render_output_contract(["project:qa/retro/retro-1/proposal-candidates.json"])
    assert "ImprovementCandidateDocumentDraftV3" in clause
    assert "schema_version '3'" in clause
    assert "omit context_sha256" in clause
    assert "include signal_ids" in clause
    assert "never emit legacy intent_key" in clause


@pytest.mark.parametrize("domain", ["issue", "workflow", "eval"])
def test_retro_signal_prompt_uses_the_agent_authored_draft_contract(domain: str) -> None:
    clause = render_output_contract([f"project:qa/retro/retro-1/signals/{domain}.json"])

    assert "SignalDraftDocument" in clause
    assert "slice_sha256" not in clause


def test_api_plan_review_prompt_declares_every_cross_skill_stop_field() -> None:
    clause = render_output_contract(["change:review/api-plan-review.json"])

    for field in (
        "review_type",
        "change_id",
        "codegen_readiness",
        "auto_fix_allowed",
        "human_review_required",
        "risk_level",
        "required_capabilities",
        "findings",
        "auto_fix_plan",
        "next_action",
    ):
        assert field in clause, field
    assert "each findings item requires id" in clause


def test_templated_output_segment_still_matches_registry_glob() -> None:
    clause = render_output_contract(["project:qa/retro/${params.retro_id}/proposal-candidates.json"])
    assert "ImprovementCandidateDocumentDraftV3" in clause


def test_duplicate_outputs_render_once() -> None:
    once = render_output_contract(["change:review/api-plan-review.json"])
    twice = render_output_contract(
        ["change:review/api-plan-review.json", "change:review/api-plan-review.json"]
    )
    assert once == twice


def test_new_required_field_appears_without_touching_the_renderer() -> None:
    from pydantic import BaseModel

    from assurance_agent.verification.contract_render import _render_model

    class Probe(BaseModel):
        brand_new_mandatory_field: str

    assert "brand_new_mandatory_field" in _render_model("x/y.json", Probe)
