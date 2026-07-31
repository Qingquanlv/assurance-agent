"""Four-layer replay selection facts and eval-layer matrix assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.eval.specialty_models import CapabilityPolicyReplayV2
from assurance_agent.eval.specialty_replay import collect_capability_policy_replay
from assurance_agent.workflow.graph.replay_binding import (
    LayerSelectionFact,
    ReplayBindingError,
    SequencedEvent,
    assert_layer_selection_evidence,
    evaluate_layer_selection,
    validate_pinned_layer_selection,
)
from assurance_agent.workflow.graph.replay_schema import validate_params_only_expression
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from tests.unit.workflow.graph.test_replay_binding import (
    _CHANGE_ID,
    _ENTRYPOINT,
    _PARAMS,
    _ROOT_INV,
    _build_fixture,
)

_SCHEMA = load_workflow_v2(Path.cwd())


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"run_mode": "api-only", "test_types": ["api", "e2e"]},
            {"api": True, "e2e": False, "fuzz": False, "performance": False},
        ),
        (
            {"run_mode": "full", "test_types": ["api", "e2e", "fuzz", "performance"]},
            {"api": True, "e2e": True, "fuzz": True, "performance": True},
        ),
        (
            {"run_mode": "full", "test_types": ["api"]},
            {"api": True, "e2e": False, "fuzz": False, "performance": False},
        ),
        (
            {"run_mode": "codegen-only", "test_types": ["api", "e2e"]},
            {"api": True, "e2e": True, "fuzz": False, "performance": False},
        ),
    ],
)
def test_evaluate_layer_selection_from_pinned_predicates(
    params: dict[str, object], expected: dict[str, bool]
) -> None:
    merged = {**_PARAMS, **params}
    facts = evaluate_layer_selection(_SCHEMA, merged)
    assert {fact.layer: fact.selected for fact in facts} == expected
    assert [fact.layer for fact in facts] == list(LAYER_NAMES)


def test_validate_pinned_layer_selection_rejects_non_params_predicate() -> None:
    assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
    api = assurance.nodes["api"].model_copy(update={"when": "state.foo == true"})
    assurance = assurance.model_copy(update={"nodes": {**assurance.nodes, "api": api}})
    schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
    errors = validate_pinned_layer_selection(schema)
    assert any("state" in error for error in errors)


def test_validate_pinned_layer_selection_rejects_node_gate_and_file_exists() -> None:
    for expr, token in (
        ("node('api').status == 'succeeded'", "node"),
        ("gate('api-plan-review-gate').verdict == 'pass'", "gate"),
        ("file_exists('repo:.aa/policy.yaml')", "file_exists"),
    ):
        assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
        api = assurance.nodes["api"].model_copy(update={"when": expr})
        assurance = assurance.model_copy(update={"nodes": {**assurance.nodes, "api": api}})
        schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
        errors = validate_pinned_layer_selection(schema)
        assert any(token in error for error in errors), expr


def test_collect_capability_policy_replay_api_only_marks_e2e_not_selected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    params = {**_PARAMS, "run_mode": "api-only", "test_types": ["api", "e2e"]}
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started" and payload.get("invocation_id") in {
            _ROOT_INV,
            fixture.assurance_inv,
        }:
            payload["params"] = params
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    replay = collect_capability_policy_replay(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert isinstance(replay, CapabilityPolicyReplayV2)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["api"].status == "complete"
    assert by_layer["e2e"].status == "not_selected"
    assert by_layer["fuzz"].status == "not_selected"
    assert by_layer["performance"].status == "not_selected"
    assert [row.layer for row in replay.rows] == list(LAYER_NAMES)


def test_collect_capability_policy_replay_selected_fuzz_is_not_wired(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    # mutate assurance params on the recorded root event to select fuzz
    params = {**_PARAMS, "test_types": ["api", "e2e", "fuzz"]}
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        payload = json.loads(line)
        if (
            payload.get("type") == "graph_invocation_started"
            and payload.get("invocation_id") == fixture.assurance_inv
        ):
            payload["params"] = params
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    replay = collect_capability_policy_replay(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "not_wired"
    assert replay.integrity == "complete"


def test_collect_capability_policy_replay_definition_failure_returns_incomplete_matrix(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    replay = collect_capability_policy_replay(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id="missing-root",
        expected_entrypoint=_ENTRYPOINT,
    )
    assert replay.integrity == "incomplete"
    assert replay.definition_binding is None
    assert replay.definition_failure == "root_invocation_unbound"
    assert len(replay.rows) == 4
    assert all(row.status == "incomplete" for row in replay.rows)


def test_params_only_validator_matches_replay_schema_guard() -> None:
    when = _SCHEMA.graphs["assurance"].nodes["api"].when or ""
    assert validate_params_only_expression(when, frozenset(_SCHEMA.params)) == ()


def test_validate_pinned_layer_selection_reports_missing_branch_node() -> None:
    assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
    nodes = dict(assurance.nodes)
    del nodes["fuzz"]
    assurance = assurance.model_copy(update={"nodes": nodes})
    schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
    errors = validate_pinned_layer_selection(schema)
    assert any("fuzz" in error and "missing" in error for error in errors)


def test_evaluate_layer_selection_raises_on_topology_incomplete_schema() -> None:
    assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
    nodes = dict(assurance.nodes)
    del nodes["performance"]
    assurance = assurance.model_copy(update={"nodes": nodes})
    schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
    with pytest.raises(ReplayBindingError, match="ambiguous_graph_wiring"):
        evaluate_layer_selection(schema, _PARAMS)


def _selection_event(
    *,
    seq: int,
    assurance_inv: str,
    layer: str,
    event_type: str,
) -> SequencedEvent:
    return SequencedEvent(
        seq=seq,
        payload={
            "type": event_type,
            "invocation_id": assurance_inv,
            "node_id": layer,
        },
    )


def test_assert_layer_selection_evidence_agrees_with_activation() -> None:
    assurance_inv = "inv-assurance"
    selections = (LayerSelectionFact(layer="api", selected=True),)
    events = (_selection_event(seq=1, assurance_inv=assurance_inv, layer="api", event_type="node_activated"),)
    assert_layer_selection_evidence(events, assurance_invocation_id=assurance_inv, selections=selections)


def test_assert_layer_selection_evidence_agrees_with_skip() -> None:
    assurance_inv = "inv-assurance"
    selections = (LayerSelectionFact(layer="e2e", selected=False),)
    events = (_selection_event(seq=1, assurance_inv=assurance_inv, layer="e2e", event_type="node_skipped"),)
    assert_layer_selection_evidence(events, assurance_invocation_id=assurance_inv, selections=selections)


def test_assert_layer_selection_evidence_rejects_predicate_event_mismatch() -> None:
    assurance_inv = "inv-assurance"
    selections = (LayerSelectionFact(layer="api", selected=True),)
    events = (_selection_event(seq=1, assurance_inv=assurance_inv, layer="api", event_type="node_skipped"),)
    with pytest.raises(ReplayBindingError, match="selection_evidence_mismatch"):
        assert_layer_selection_evidence(events, assurance_invocation_id=assurance_inv, selections=selections)


def test_assert_layer_selection_evidence_allows_missing_events() -> None:
    assurance_inv = "inv-assurance"
    selections = (
        LayerSelectionFact(layer="api", selected=True),
        LayerSelectionFact(layer="e2e", selected=False),
    )
    assert_layer_selection_evidence((), assurance_invocation_id=assurance_inv, selections=selections)


def test_bind_replay_definitions_exposes_selected_layers_and_topologies(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.replay_binding import bind_replay_definitions
    from assurance_agent.workflow.graph.workspace import TreeStore

    fixture = _build_fixture(tmp_path, include_e2e=False)
    params = {**_PARAMS, "run_mode": "full", "test_types": ["api", "fuzz"]}
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started" and payload.get("invocation_id") in {
            _ROOT_INV,
            fixture.assurance_inv,
        }:
            payload["params"] = params
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
        store=TreeStore(fixture.change_dir),
    )
    assert binding.selected_layers == frozenset({"api", "fuzz"})
    assert binding.layer_topologies["api"].status == "wired"
    assert binding.layer_topologies["fuzz"].status == "legacy_unwired"
    assert binding.layer_topologies["e2e"].status == "wired"
