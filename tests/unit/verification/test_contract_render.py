"""OUTPUT CONTRACT 子句从 artifact registry 的 pydantic 模型渲染（spec C1）。"""

from types import SimpleNamespace
from typing import Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict

from assurance_agent.verification import contract_render
from assurance_agent.verification.contract_render import _ShapeRenderer, render_output_contract


_STRICT = ConfigDict(extra="forbid")


def _render_probe(
    monkeypatch: pytest.MonkeyPatch,
    model: type[BaseModel],
    *,
    authoring_model: type[BaseModel] | None = None,
) -> str:
    spec = SimpleNamespace(model=model, authoring_model=authoring_model)
    monkeypatch.setattr(contract_render, "match_artifact", lambda _rel: spec)
    return render_output_contract(["project:probe.json"])


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


def test_improvement_assessment_renders_strict_nested_finding_shape() -> None:
    clause = render_output_contract(["project:qa/improvements/reviews/review-1/assessment.json"])

    assert "root object (allowed fields (*=required" in clause
    assert "decision*" in clause
    assert "review_id" not in clause
    assert "improvement_id" not in clause
    assert "expected_improvement_version" not in clause
    assert "subject_sha256" not in clause
    assert "no undeclared fields" in clause
    assert "notes" not in clause
    assert "findings[] -> D1=AutoReviewFinding" in clause
    assert "D1 object (allowed fields (*=required" in clause
    assert "finding_id*, severity*, category*, message*, source_refs" in clause
    assert 'D1.severity enum ["info","low","medium","high","critical","blocking"]' in clause
    assert "D1.source_refs optional array<string>" in clause


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


def test_case_yaml_authoring_prompt_names_performance_execution_identity() -> None:
    clause = render_output_contract(["change:cases/system/performance/case.yaml"])

    assert "Performance entries require" in clause
    assert "automation.performance.scenario.capability" in clause
    assert "automation.performance.scenario.endpoint" in clause
    assert "non-empty strings" in clause


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


def test_strict_parent_expands_fixed_tuple_child_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child(BaseModel):
        model_config = _STRICT

        name: str
        kind: Literal["primary", "secondary"]

    class Parent(BaseModel):
        model_config = _STRICT

        children: tuple[Child]

    clause = _render_probe(monkeypatch, Parent)

    assert "root object (allowed fields (*=required): children*" in clause
    assert "no undeclared fields" in clause
    assert "children fixed tuple (minItems=1, maxItems=1)" in clause
    assert "children[0] -> D1=Child" in clause
    assert "D1 object (allowed fields (*=required): name*, kind*" in clause
    assert 'D1.kind enum ["primary","secondary"]' in clause
    assert "children[]" not in clause


def test_optional_nullable_child_is_distinguished_from_requiredness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child(BaseModel):
        model_config = _STRICT

        value: str

    class Parent(BaseModel):
        model_config = _STRICT

        child: Child | None = None

    clause = _render_probe(monkeypatch, Parent)

    assert "root object (allowed fields (*=required, ?=nullable): child?" in clause
    assert "child optional nullable -> D1=Child" in clause
    assert "D1 object (allowed fields (*=required): value*" in clause


def test_self_reference_is_described_once_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Node(BaseModel):
        model_config = _STRICT

        value: str
        children: tuple[Self, ...] = ()

    clause = _render_probe(monkeypatch, Node)

    assert clause.count("root object (allowed fields (*=required): value*, children") == 1
    assert "children[] -> root" in clause
    assert len(clause) < 1_000


def test_authoring_model_nested_shape_wins_over_runtime_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeChild(BaseModel):
        runtime_only: str

    class RuntimeDocument(BaseModel):
        payload: RuntimeChild

    class AuthorChild(BaseModel):
        authored: str

    class AuthorDocument(BaseModel):
        payload: AuthorChild

    clause = _render_probe(
        monkeypatch,
        RuntimeDocument,
        authoring_model=AuthorDocument,
    )

    assert "must be a AuthorDocument" in clause
    assert "payload -> D1=AuthorChild" in clause
    assert "D1 object (allowed fields (*=required): authored*)" in clause
    assert "runtime_only" not in clause


def test_map_values_expand_reachable_strict_child_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child(BaseModel):
        model_config = _STRICT

        child_id: str
        kind: Literal["first", "second"]

    class Parent(BaseModel):
        model_config = _STRICT

        entries: dict[str, Child]

    clause = _render_probe(monkeypatch, Parent)

    assert "entries object (map values)" in clause
    assert "entries{*} -> D1=Child" in clause
    assert "D1 object (allowed fields (*=required): child_id*, kind*" in clause
    assert "no undeclared fields" in clause
    assert 'D1.kind enum ["first","second"]' in clause
    assert "entries object (allowed fields" not in clause


def test_boolean_schemas_are_rendered_in_properties_items_unions_and_defs() -> None:
    schema = {
        "$defs": {"Anything": True, "Nothing": False},
        "type": "object",
        "properties": {
            "anything": True,
            "nothing": False,
            "anything_ref": {"$ref": "#/$defs/Anything"},
            "nothing_ref": {"$ref": "#/$defs/Nothing"},
            "values": {"type": "array", "items": False},
            "choice": {"anyOf": [True, {"type": "string"}]},
        },
        "required": [
            "anything",
            "nothing",
            "anything_ref",
            "nothing_ref",
            "values",
            "choice",
        ],
    }

    rendered = "; ".join(_ShapeRenderer(schema).render())

    assert "anything any value" in rendered
    assert "nothing forbidden (no value)" in rendered
    assert "anything_ref -> D1=Anything" in rendered
    assert "D1 any value" in rendered
    assert "nothing_ref -> D2=Nothing" in rendered
    assert "D2 forbidden (no value)" in rendered
    assert "values[] forbidden (no value)" in rendered
    assert "choice union" in rendered
    assert "choice<variant 1> any value" in rendered
    assert "choice<variant 2> string" in rendered


def test_boolean_additional_properties_describe_open_and_closed_maps() -> None:
    schema = {
        "type": "object",
        "properties": {
            "open_map": {"type": "object", "additionalProperties": True},
            "closed_map": {"type": "object", "additionalProperties": False},
        },
        "required": ["open_map", "closed_map"],
    }

    rendered = "; ".join(_ShapeRenderer(schema).render())

    assert "open_map object (map values: any value)" in rendered
    assert "open_map object (allowed fields" not in rendered
    assert "closed_map object (allowed fields: (none)" in rendered
    assert "required fields: (none); no undeclared fields" in rendered


def test_reused_inline_object_keeps_each_paths_optional_nullable_modifiers() -> None:
    shared_child = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    schema = {
        "type": "object",
        "properties": {
            "optional_child": shared_child,
            "nullable_child": {"anyOf": [shared_child, {"type": "null"}]},
        },
        "required": ["nullable_child"],
    }

    rendered = "; ".join(_ShapeRenderer(schema).render())

    assert "optional_child optional object" in rendered
    assert "optional_child optional object (allowed fields (*=required): value*)" in rendered
    assert "nullable_child nullable object" in rendered
    assert "nullable_child nullable object (allowed fields (*=required): value*)" in rendered


def test_enum_and_const_values_use_unambiguous_json_literals() -> None:
    schema = {
        "type": "object",
        "properties": {
            "choice": {"enum": ["a,b", 'say "hi"', 2, True]},
            "enabled": {"const": True},
            "version": {"const": 3},
        },
        "required": ["choice", "enabled", "version"],
    }

    rendered = "; ".join(_ShapeRenderer(schema).render())

    assert 'choice enum ["a,b","say \\"hi\\"",2,true]' in rendered
    assert "enabled const true" in rendered
    assert "version const 3" in rendered
    assert "const 'true'" not in rendered
    assert "const '3'" not in rendered


@pytest.mark.parametrize("domain", ["issue", "workflow", "eval"])
def test_real_retro_signal_contract_stays_compact(domain: str) -> None:
    clause = render_output_contract([f"project:qa/retro/retro-1/signals/{domain}.json"])

    assert len(clause) < 7_000


def test_real_improvement_outbox_contract_stays_compact() -> None:
    clause = render_output_contract(["project:qa/improvements/outbox/pending/item-1.json"])

    assert len(clause) < 17_000
