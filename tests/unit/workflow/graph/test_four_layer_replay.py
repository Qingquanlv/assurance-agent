"""Four-layer replay selection facts and eval-layer matrix assembly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.eval.specialty_models import (
    CapabilityPolicyReplayV2,
    CompleteLayerRow,
    build_capability_replay_v2,
)
from assurance_agent.eval.specialty_replay import collect_capability_policy_replay, replay_wired_layer
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.replay_binding import (
    FrozenDefinitionBinding,
    LayerSelectionFact,
    ReplayBindingError,
    SequencedEvent,
    assert_layer_selection_evidence,
    bind_replay_definitions,
    evaluate_layer_selection,
    recover_layer_inputs,
    validate_pinned_layer_selection,
)
from assurance_agent.verification.profile_manifest import assurance_profile_bytes
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.historical_roles import fixture_roles_from_schema
from assurance_agent.workflow.graph.replay_schema import PinnedLayerTopology, validate_params_only_expression
from assurance_agent.workflow.graph.schema_v2 import EdgeDef, GraphDef, load_workflow_v2
from assurance_agent.workflow.orchestration.plan_check_replay import replay_plan_check_policy
from tests.unit.workflow.graph.test_replay_binding import (
    _CHANGE_ID,
    _ENTRYPOINT,
    _PARAMS,
    _ROOT_INV,
    _build_fixture,
    _stage_pinned_definition_snapshots,
    _stage_profile_snapshot,
)

_SCHEMA = load_workflow_v2(Path.cwd())
_ROLES = fixture_roles_from_schema(_SCHEMA)


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
    facts = evaluate_layer_selection(_SCHEMA, merged, historical_roles=_ROLES)
    assert {fact.layer: fact.selected for fact in facts} == expected
    assert [fact.layer for fact in facts] == list(LAYER_NAMES)


def test_validate_pinned_layer_selection_rejects_non_params_predicate() -> None:
    assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
    api = assurance.nodes["api"].model_copy(update={"when": "state.foo == true"})
    assurance = assurance.model_copy(update={"nodes": {**assurance.nodes, "api": api}})
    schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
    errors = validate_pinned_layer_selection(schema, historical_roles=_ROLES)
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
        errors = validate_pinned_layer_selection(schema, historical_roles=_ROLES)
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


def test_collect_capability_policy_replay_selected_fuzz_is_incomplete_without_snapshot(
    tmp_path: Path,
) -> None:
    """v4 current packaged Fuzz is wired but force-partial without a profile snapshot."""
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
    assert by_layer["fuzz"].status == "incomplete"
    assert by_layer["fuzz"].reason_code == "partial_assurance_wiring"
    assert replay.integrity == "incomplete"


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
    from assurance_agent.workflow.graph.historical_roles import discover_historical_assurance_roles

    roles, _issues = discover_historical_assurance_roles(schema)
    assert roles is not None
    errors = validate_pinned_layer_selection(schema, historical_roles=roles)
    assert any("fuzz" in error and "missing" in error for error in errors)


def test_evaluate_layer_selection_raises_on_topology_incomplete_schema() -> None:
    assurance = _SCHEMA.graphs["assurance"].model_copy(deep=True)
    nodes = dict(assurance.nodes)
    del nodes["performance"]
    assurance = assurance.model_copy(update={"nodes": nodes})
    schema = _SCHEMA.model_copy(update={"graphs": {**_SCHEMA.graphs, "assurance": assurance}})
    from assurance_agent.workflow.graph.historical_roles import discover_historical_assurance_roles

    roles, _issues = discover_historical_assurance_roles(schema)
    assert roles is not None
    with pytest.raises(ReplayBindingError, match="ambiguous_graph_wiring"):
        evaluate_layer_selection(schema, _PARAMS, historical_roles=roles)


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
    assert_layer_selection_evidence(
        events,
        assurance_invocation_id=assurance_inv,
        selections=selections,
        historical_roles=_ROLES,
    )


def test_assert_layer_selection_evidence_agrees_with_skip() -> None:
    assurance_inv = "inv-assurance"
    selections = (LayerSelectionFact(layer="e2e", selected=False),)
    events = (_selection_event(seq=1, assurance_inv=assurance_inv, layer="e2e", event_type="node_skipped"),)
    assert_layer_selection_evidence(
        events,
        assurance_invocation_id=assurance_inv,
        selections=selections,
        historical_roles=_ROLES,
    )


def test_assert_layer_selection_evidence_rejects_predicate_event_mismatch() -> None:
    assurance_inv = "inv-assurance"
    selections = (LayerSelectionFact(layer="api", selected=True),)
    events = (_selection_event(seq=1, assurance_inv=assurance_inv, layer="api", event_type="node_skipped"),)
    with pytest.raises(ReplayBindingError, match="selection_evidence_mismatch"):
        assert_layer_selection_evidence(
            events,
            assurance_invocation_id=assurance_inv,
            selections=selections,
            historical_roles=_ROLES,
        )


def test_assert_layer_selection_evidence_allows_missing_events() -> None:
    assurance_inv = "inv-assurance"
    selections = (
        LayerSelectionFact(layer="api", selected=True),
        LayerSelectionFact(layer="e2e", selected=False),
    )
    assert_layer_selection_evidence(
        (),
        assurance_invocation_id=assurance_inv,
        selections=selections,
        historical_roles=_ROLES,
    )


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
    assert binding.layer_topologies["fuzz"].status == "partial"
    assert binding.layer_topologies["e2e"].status == "wired"


def _partial_fuzz_plan_review_cycle(schema):
    """Keep activation markers but mis-route knowledge resume → topology partial."""
    cycle = schema.graphs["fuzz-plan-cycle"]
    routes = [
        route
        if route.from_ != "knowledge-remediation"
        else route.model_copy(update={"cases": {**route.cases, "fix_and_proceed": "review"}})
        for route in cycle.routes
    ]
    graphs = {
        **schema.graphs,
        "fuzz-plan-cycle": cycle.model_copy(update={"routes": routes}),
    }
    return schema.model_copy(update={"graphs": graphs})


def _zero_marker_fuzz_plan_review_cycle(schema):
    """True legacy: keep a compileable cycle with zero activation markers."""
    cycle = schema.graphs["fuzz-plan-cycle"]
    stub = GraphDef(
        max_supersteps=cycle.max_supersteps,
        nodes={"review": cycle.nodes["review"]},
        edges=[
            EdgeDef(**{"from": "START", "to": "review"}),
            EdgeDef(**{"from": "review", "to": "END"}),
        ],
    )
    return schema.model_copy(update={"graphs": {**schema.graphs, "fuzz-plan-cycle": stub}})


def _repin_schema(fixture, schema) -> None:
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_workflow(schema, contracts)
    old_digest = fixture.compiled.digest  # type: ignore[attr-defined]
    _stage_pinned_definition_snapshots(
        fixture.change_dir,
        compiled=compiled,
        schema=schema,
        contracts=contracts,
    )
    rewritten: list[str] = []
    for line in fixture.events_path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("graph_digest") == old_digest:
            payload["graph_digest"] = compiled.digest
        if payload.get("ir_digest") == old_digest:
            payload["ir_digest"] = compiled.digest
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    fixture.compiled = compiled  # type: ignore[attr-defined]


def _repin_partial_fuzz_topology(fixture) -> None:
    _repin_schema(fixture, _partial_fuzz_plan_review_cycle(load_workflow_v2(Path.cwd())))


def _repin_zero_marker_fuzz_topology(fixture) -> None:
    _repin_schema(fixture, _zero_marker_fuzz_plan_review_cycle(load_workflow_v2(Path.cwd())))


def _upgrade_fixture_to_v5(fixture) -> None:
    profile_bytes = assurance_profile_bytes()
    profile_digest = hashlib.sha256(profile_bytes).hexdigest()
    _stage_profile_snapshot(fixture.change_dir, profile_digest, profile_bytes)
    rewritten: list[str] = []
    for line in fixture.events_path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["event_schema_version"] = 5
            payload["assurance_profile_digest"] = profile_digest
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def _write_pass_shaped_active_files(change_dir: Path, layer: str) -> None:
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    for relative in (profile.review_artifact, profile.checks_artifact):
        path = change_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"pass","checks":[]}\n', encoding="utf-8")


def _rewrite_assurance_params(fixture, params: dict[str, object]) -> None:
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


def _collect(fixture) -> CapabilityPolicyReplayV2:
    return collect_capability_policy_replay(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )


def test_collect_emits_replay_semantics_v2(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    replay = _collect(fixture)
    assert replay.semantics == "counterfactual_plan_check_actions/v2"


def test_v4_selected_fuzz_without_snapshot_is_incomplete_even_with_stray_active_files(
    tmp_path: Path,
) -> None:
    """Packaged Fuzz without a profile snapshot is force-partial, not legacy not_wired."""
    fixture = _build_fixture(tmp_path)
    _rewrite_assurance_params(fixture, {**_PARAMS, "test_types": ["api", "e2e", "fuzz"]})
    fuzz_profile = get_layer_assurance_profile("fuzz")
    for relative in (fuzz_profile.review_artifact, fuzz_profile.checks_artifact):
        path = fixture.change_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"pass","checks":[]}\n', encoding="utf-8")

    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "incomplete"
    assert by_layer["fuzz"].reason_code == "partial_assurance_wiring"
    assert replay.integrity == "incomplete"
    assert replay.semantics == "counterfactual_plan_check_actions/v2"


def test_collect_zero_marker_selected_fuzz_is_not_wired(tmp_path: Path) -> None:
    """Pinned zero-marker specialty schema yields true collect-path not_wired."""
    fixture = _build_fixture(tmp_path, include_e2e=False)
    _repin_zero_marker_fuzz_topology(fixture)
    _rewrite_assurance_params(fixture, {**_PARAMS, "run_mode": "full", "test_types": ["api", "fuzz"]})

    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.layer_topologies["fuzz"].status == "legacy_unwired"

    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "not_wired"
    assert by_layer["fuzz"].reason_code is None
    assert by_layer["api"].status == "complete"
    assert replay.integrity == "complete"
    assert replay.semantics == "counterfactual_plan_check_actions/v2"


def test_collect_zero_marker_selected_fuzz_is_not_wired_even_with_stray_active_files(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    _repin_zero_marker_fuzz_topology(fixture)
    _rewrite_assurance_params(fixture, {**_PARAMS, "run_mode": "full", "test_types": ["api", "fuzz"]})
    _write_pass_shaped_active_files(fixture.change_dir, "fuzz")

    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "not_wired"
    assert by_layer["fuzz"].reason_code is None
    assert replay.integrity == "complete"


def test_v4_legacy_unselected_performance_is_not_selected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    replay = _collect(fixture)
    assert {row.layer: row.status for row in replay.rows}["performance"] == "not_selected"


def test_v4_forged_wired_fuzz_without_profile_snapshot_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.graph import replay_binding as replay_mod
    from assurance_agent.workflow.graph.replay_schema import classify_pinned_layer_topology_v4

    fixture = _build_fixture(tmp_path, include_e2e=False)
    _rewrite_assurance_params(fixture, {**_PARAMS, "run_mode": "full", "test_types": ["api", "fuzz"]})
    original = classify_pinned_layer_topology_v4

    def classify_force_wired(schema, topology_spec):  # type: ignore[no-untyped-def]
        topology = original(schema, topology_spec)
        if topology_spec.layer == "fuzz":
            return PinnedLayerTopology(
                layer="fuzz",
                status="wired",
                assurance_node_id="fuzz",
                branch_graph_id="fuzz-branch",
                cycle_call_node_id="review-cycle",
                cycle_graph_id="fuzz-plan-cycle",
                applicability_node_id="applicability",
                reviewer_node_id="review",
                mechanical_node_id="mechanical-plan-checks",
                gate_node_id="review-gate",
                human_review_node_id="human-review",
                knowledge_remediation_node_id="knowledge-remediation",
                codegen_precondition_node_id="codegen-precheck",
                codegen_node_id="codegen",
                diagnostics=(),
                semantics_id="legacy_v4_unbound",
                semantics_bound=False,
            )
        return topology

    monkeypatch.setattr(replay_mod, "classify_pinned_layer_topology_v4", classify_force_wired)
    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "incomplete"
    assert by_layer["fuzz"].reason_code == "partial_assurance_wiring"


def test_v5_partial_fuzz_with_pass_shaped_active_files_is_incomplete(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    _repin_partial_fuzz_topology(fixture)
    _upgrade_fixture_to_v5(fixture)
    _rewrite_assurance_params(fixture, {**_PARAMS, "run_mode": "full", "test_types": ["api", "fuzz"]})
    _write_pass_shaped_active_files(fixture.change_dir, "fuzz")

    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.event_schema_version >= 5
    assert binding.layer_topologies["fuzz"].status == "partial"
    assert binding.layer_topologies["fuzz"].diagnostics

    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["fuzz"].status == "incomplete"
    assert by_layer["fuzz"].reason_code == "partial_assurance_wiring"
    assert replay.integrity == "incomplete"
    assert replay.semantics == "counterfactual_plan_check_actions/v2"


def test_v5_missing_profile_snapshot_is_definition_incomplete(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["event_schema_version"] = 5
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    replay = _collect(fixture)
    assert replay.integrity == "incomplete"
    assert replay.definition_binding is None
    assert replay.definition_failure == "profile_snapshot_missing"
    assert all(row.status == "incomplete" for row in replay.rows)


def test_broken_pinned_binding_never_upgrades_to_not_wired(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    digest = json.loads(fixture.events_path.read_text(encoding="utf-8").splitlines()[0])["graph_digest"]
    (fixture.change_dir / ".graph-runtime" / "schemas" / f"{digest}.json").unlink()
    replay = _collect(fixture)
    assert replay.integrity == "incomplete"
    assert replay.definition_failure == "pinned_schema_missing"
    assert all(row.status == "incomplete" for row in replay.rows)
    assert not any(row.status == "not_wired" for row in replay.rows)


def test_wired_layer_ambiguous_child_invocation_is_incomplete(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    assurance_path = "main/assurance/assurance"
    api_branch_path = f"{assurance_path}/api/api-branch"
    duplicate = fixture._started(
        invocation_id="inv-api-branch-dup",
        entrypoint="api-branch",
        graph_id="api-branch",
        structural_path=api_branch_path,
        checkpoint_ns="dup-api-branch",
        params=dict(_PARAMS),
        parent_invocation_id=fixture.assurance_inv,
        parent_task_id=f"{assurance_path}:api",
    )
    fixture.append(duplicate.model_dump(mode="json"))
    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["api"].status == "incomplete"
    assert by_layer["api"].reason_code == "ambiguous_graph_wiring"


def test_wired_layer_missing_evidence_is_incomplete(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.replay_binding import normalize_logical_path

    fixture = _build_fixture(tmp_path, include_e2e=False)
    profile = get_layer_assurance_profile("api")
    checks_path = normalize_logical_path(f"change:{profile.checks_artifact}")
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        task_id = str(payload.get("task_id", ""))
        if payload.get("type") == "task_attempt_succeeded" and task_id.endswith(":mechanical-plan-checks"):
            if "api-plan-cycle" in task_id:
                payload = dict(payload)
                payload["outputs_sha256"] = {checks_path: "0" * 64}
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    replay = _collect(fixture)
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["api"].status == "incomplete"
    assert by_layer["api"].reason_code == "mechanical_producer_unbound"


def test_frozen_semantics_v1_rejects_forged_fuzz_complete_row() -> None:
    rows = [
        {
            "layer": "api",
            "case_type": "API",
            "status": "not_selected",
            "reason_code": None,
        },
        {
            "layer": "e2e",
            "case_type": "E2E",
            "status": "not_selected",
            "reason_code": None,
        },
        {
            "layer": "fuzz",
            "case_type": "Fuzz",
            "status": "complete",
            "reason_code": None,
            "applicability": "applicable",
            "gate_id": "fuzz-plan-review-gate",
            "review_artifact": "review/fuzz-plan-review.json",
            "checks_artifact": "review/fuzz-plan-checks.json",
            "capabilities": {"required": ["auth.api_admin_token"], "missing": []},
            "mechanical_checks": {
                "status": "pass",
                "finding_count": 0,
                "checks": [
                    {
                        "check_id": check_id,
                        "status": "not_applicable" if check_id == "assert_ideal" else "pass",
                        "finding_count": 0,
                    }
                    for check_id in PLAN_CHECK_IDS
                ],
            },
            "mechanical_execution_contract_digest": "mech-fuzz",
            "evidence_digests": {
                "review": "review-fuzz",
                "checks": "checks-fuzz",
                "data_knowledge": "l1-fuzz",
            },
            "scenarios": [
                {
                    "action": action,
                    "policy_digest": f"d-{action}",
                    "verdict": "pass",
                    "route": "pass",
                    "matched_rule": "pass_when",
                    "reason": "ok",
                    "missing_capabilities": [],
                    "policy_effect": "no_failed_checks",
                }
                for action in ("warn", "block", "require_human")
            ],
        },
        {
            "layer": "performance",
            "case_type": "Performance",
            "status": "not_selected",
            "reason_code": None,
        },
    ]
    with pytest.raises(ValidationError, match="cannot have status complete"):
        build_capability_replay_v2(
            definition_binding={
                "root_invocation_id": "inv-root",
                "assurance_invocation_id": "inv-assurance",
                "graph_digest": "graph",
                "gate_definition_source": "pinned_schema",
                "baseline_policy_digest": "policy",
                "policy_source": "pinned_runtime_snapshot",
                "policy_origin": "project",
                "gate_semantics_digest": "semantics",
                "assurance_profile_digest": "profile",
            },
            rows=rows,
            semantics="counterfactual_plan_check_actions/v1",
        )


def _synthetic_wired_specialty_binding(
    binding: FrozenDefinitionBinding, *, layer: str
) -> FrozenDefinitionBinding:
    api_topo = binding.layer_topologies["api"]
    specialty_topo = PinnedLayerTopology(
        layer=layer,
        status="wired",
        assurance_node_id=layer,
        branch_graph_id=api_topo.branch_graph_id,
        cycle_call_node_id=api_topo.cycle_call_node_id,
        cycle_graph_id=api_topo.cycle_graph_id,
        applicability_node_id=api_topo.applicability_node_id,
        reviewer_node_id=api_topo.reviewer_node_id,
        mechanical_node_id=api_topo.mechanical_node_id,
        gate_node_id=api_topo.gate_node_id,
        human_review_node_id=api_topo.human_review_node_id,
        knowledge_remediation_node_id=api_topo.knowledge_remediation_node_id,
        codegen_precondition_node_id=api_topo.codegen_precondition_node_id,
        codegen_node_id=api_topo.codegen_node_id,
        diagnostics=(),
    )
    return replace(
        binding,
        selected_layers=frozenset({*binding.selected_layers, layer}),
        layer_topologies={**binding.layer_topologies, layer: specialty_topo},
        profile_compatibility={**binding.profile_compatibility, layer: True},
    )


@pytest.mark.parametrize(
    ("layer", "case_type"),
    [("fuzz", "Fuzz"), ("performance", "Performance")],
)
@pytest.mark.parametrize("applicable", [True, False])
def test_synthetic_frozen_binding_builds_complete_specialty_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    applicable: bool,
    layer: str,
    case_type: str,
) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False, api_applicable=applicable)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    api_inputs = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    api_profile = get_layer_assurance_profile("api")
    api_replay = replay_plan_check_policy(
        gates=binding.compiled.schema.gates,
        profile=api_profile,
        review=api_inputs.review,
        checks=api_inputs.checks,
        data_knowledge=api_inputs.data_knowledge,
        base_policy=binding.policy,
        change_id=binding.change_id,
        params=api_inputs.params,
    )
    synthetic = _synthetic_wired_specialty_binding(binding, layer=layer)

    monkeypatch.setattr(
        "assurance_agent.eval.specialty_replay.recover_layer_inputs",
        lambda _binding, *, layer, change_dir, store=None: api_inputs,
    )
    monkeypatch.setattr(
        "assurance_agent.eval.specialty_replay.get_layer_assurance_profile",
        lambda layer: api_profile,
    )
    monkeypatch.setattr(
        "assurance_agent.eval.specialty_replay.replay_plan_check_policy",
        lambda **_kwargs: api_replay,
    )

    row = replay_wired_layer(
        synthetic,
        layer=layer,
        case_type=case_type,
        change_dir=fixture.change_dir,
        store=None,
    )
    assert isinstance(row, CompleteLayerRow)
    assert row.status == "complete"
    assert row.layer == layer
    assert [item.check_id for item in row.mechanical_checks.checks] == list(PLAN_CHECK_IDS)
    assert [item.action for item in row.scenarios] == ["warn", "block", "require_human"]
    if applicable:
        assert row.applicability == "applicable"
        assert row.capabilities is not None
        assert row.evidence_digests.review is not None
        assert row.evidence_digests.data_knowledge is not None
        assert row.evidence_digests.checks
        assert row.mechanical_execution_contract_digest
    else:
        assert row.applicability == "not_applicable"
        assert row.capabilities is None
        assert row.evidence_digests.review is None
        assert row.evidence_digests.data_knowledge is None
        assert row.evidence_digests.checks
        assert all(item.status == "not_applicable" for item in row.mechanical_checks.checks)
        assert all(item.verdict == "skip" for item in row.scenarios)
