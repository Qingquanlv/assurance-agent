"""Historical role discovery and v4/v5/v6 topology classification (Task 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.compiler import (
    HistoricalCompileContext,
    compile_historical_workflow,
)
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_agent.workflow.graph.historical_roles import (
    discover_historical_assurance_roles,
    fixture_roles_from_schema,
)
from assurance_agent.workflow.graph.historical_topology_v6 import V6_SEMANTICS_ID
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.replay_binding import evaluate_layer_selection
from assurance_agent.workflow.graph.replay_schema import (
    LayerTopologySpec,
    classify_pinned_layer_topology_v4,
    classify_pinned_layer_topology_v5,
    classify_pinned_layer_topology_v6,
)
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2, load_workflow_v2
from assurance_agent.workflow.graph.topology_analysis import reorder_commutative_and


def _schema() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd())


def _topology_spec(layer: str) -> LayerTopologySpec:
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    return LayerTopologySpec(
        layer=profile.layer,
        plan_artifacts=profile.plan_artifacts,
        review_artifact=profile.review_artifact,
        review_alias=profile.review_alias,
        checks_artifact=profile.checks_artifact,
        gate_id=profile.gate_id,
    )


def _strip_activation_markers(schema: WorkflowSchemaV2, layer: str) -> WorkflowSchemaV2:
    cycle_id = f"{layer}-plan-cycle"
    cycle = schema.graphs[cycle_id]
    drop = {"applicability", "mechanical-plan-checks", "review-gate"}
    nodes = {k: v for k, v in cycle.nodes.items() if k not in drop}
    return schema.model_copy(
        update={"graphs": {**schema.graphs, cycle_id: cycle.model_copy(update={"nodes": nodes})}}
    )


def _add_direct_codegen_bypass(schema: WorkflowSchemaV2, layer: str) -> WorkflowSchemaV2:
    branch_id = f"{layer}-branch"
    branch = schema.graphs[branch_id]
    from assurance_agent.workflow.graph.schema_v2 import EdgeDef

    edges = list(branch.edges) + [EdgeDef.model_validate({"from": "review-cycle", "to": "codegen"})]
    return schema.model_copy(
        update={"graphs": {**schema.graphs, branch_id: branch.model_copy(update={"edges": edges})}}
    )


def _rename_graph_ids(schema: WorkflowSchemaV2, mapping: dict[str, str]) -> WorkflowSchemaV2:
    graphs = {}
    for graph_id, graph in schema.graphs.items():
        new_id = mapping.get(graph_id, graph_id)
        nodes = {}
        for node_id, node in graph.nodes.items():
            uses = node.uses
            if uses.startswith("graph:"):
                callee = uses.removeprefix("graph:")
                uses = f"graph:{mapping.get(callee, callee)}"
            nodes[node_id] = node.model_copy(update={"uses": uses})
        graphs[new_id] = graph.model_copy(update={"nodes": nodes})
    entrypoints = {
        name: ep.model_copy(update={"graph": mapping.get(ep.graph, ep.graph)})
        for name, ep in schema.entrypoints.items()
    }
    return schema.model_copy(update={"graphs": graphs, "entrypoints": entrypoints})


def test_v4_v5_golden_semantics_pairs_frozen() -> None:
    schema = _schema()
    for layer in ("api", "e2e", "fuzz", "performance"):
        spec = _topology_spec(layer)
        v4 = classify_pinned_layer_topology_v4(schema, spec)
        v5 = classify_pinned_layer_topology_v5(schema, spec)
        assert (v4.semantics_id, v4.semantics_bound) == ("legacy_v4_unbound", False)
        assert (v5.semantics_id, v5.semantics_bound) == ("legacy_v5_unbound", False)
        assert v4.status == v5.status == "wired"

    legacy = _strip_activation_markers(schema, "fuzz")
    bypass = _add_direct_codegen_bypass(schema, "api")
    for classifier, semantics_id in (
        (classify_pinned_layer_topology_v4, "legacy_v4_unbound"),
        (classify_pinned_layer_topology_v5, "legacy_v5_unbound"),
    ):
        legacy_topo = classifier(legacy, _topology_spec("fuzz"))
        bypass_topo = classifier(bypass, _topology_spec("api"))
        assert (legacy_topo.semantics_id, legacy_topo.semantics_bound) == (semantics_id, False)
        assert legacy_topo.status == "legacy_unwired"
        assert (bypass_topo.semantics_id, bypass_topo.semantics_bound) == (semantics_id, False)
        # Frozen v5/v4 may miss this bypass (known false negative); status is frozen as-is.
        assert bypass_topo.status in {"wired", "partial"}


def test_renamed_safe_graph_one_manifest_drives_compile_selection_and_v6() -> None:
    schema = _schema()
    graph_map = {
        "assurance": "hist-assurance",
        "api-branch": "hist-api-branch",
        "api-plan-cycle": "hist-api-cycle",
        "e2e-branch": "hist-e2e-branch",
        "e2e-plan-cycle": "hist-e2e-cycle",
        "fuzz-branch": "hist-fuzz-branch",
        "fuzz-plan-cycle": "hist-fuzz-cycle",
        "performance-branch": "hist-perf-branch",
        "performance-plan-cycle": "hist-perf-cycle",
    }
    renamed = _rename_graph_ids(schema, graph_map)
    node_map = {
        "api": "sel-api",
        "e2e": "sel-e2e",
        "fuzz": "sel-fuzz",
        "performance": "sel-perf",
    }
    # Rename only selection nodes inside the already-renamed assurance graph.
    assurance_id = graph_map["assurance"]
    assurance = renamed.graphs[assurance_id]
    nodes: dict = {}
    for nid, node in assurance.nodes.items():
        new_id = node_map.get(nid, nid)
        if node.join is not None:
            sources = [node_map.get(item, item) for item in node.join.sources]
            node = node.model_copy(update={"join": node.join.model_copy(update={"sources": sources})})
        nodes[new_id] = node
    edges = [
        edge.model_copy(
            update={
                "from_": node_map.get(edge.from_, edge.from_),
                "to": node_map.get(edge.to, edge.to),
            }
        )
        for edge in assurance.edges
    ]
    routes = []
    for route in assurance.routes:
        cases = {label: node_map.get(target, target) for label, target in route.cases.items()}
        default = None if route.default is None else node_map.get(route.default, route.default)
        routes.append(
            route.model_copy(
                update={
                    "from_": node_map.get(route.from_, route.from_),
                    "cases": cases,
                    "default": default,
                }
            )
        )
    renamed = renamed.model_copy(
        update={
            "graphs": {
                **renamed.graphs,
                assurance_id: assurance.model_copy(update={"nodes": nodes, "edges": edges, "routes": routes}),
            }
        }
    )

    roles, issues = discover_historical_assurance_roles(renamed)
    assert not [issue for issue in issues if issue.code in {"duplicate_role", "ambiguous_alias"}]
    assert roles is not None
    assert roles.assurance_graph_id == "hist-assurance"
    assert {item.layer for item in roles.layers} == {"api", "e2e", "fuzz", "performance"}
    assert all(item.branch_graph_id.startswith("hist-") for item in roles.layers)
    digest = roles.canonical_digest
    roles_again, _ = discover_historical_assurance_roles(renamed)
    assert roles_again is not None
    assert roles_again.canonical_digest == digest

    # Selection uses discovered node ids, not current names.
    facts = evaluate_layer_selection(
        renamed,
        {"run_mode": "full", "test_types": ["api", "e2e"]},
        historical_roles=roles,
    )
    assert {fact.layer: fact.selected for fact in facts} == {
        "api": True,
        "e2e": True,
        "fuzz": False,
        "performance": False,
    }

    from assurance_agent.workflow.graph.compiler import canonical_digest
    from assurance_agent.workflow.graph.replay_schema import validate_historical_replay_surface

    assert validate_historical_replay_surface(renamed, historical_roles=roles) == ()
    contracts = load_execution_contracts(Path.cwd())
    ingest = validate_catalog_runtime()
    current_targets = {
        node.uses
        for graph in renamed.graphs.values()
        for node in graph.nodes.values()
        if not node.uses.startswith("graph:") and node.uses in contracts.contracts
    }
    pinned = ExecutionContractCatalog(
        contracts={target: contracts.contracts[target] for target in current_targets}
    )
    compiled = compile_historical_workflow(
        renamed,
        context=HistoricalCompileContext(
            ingest_catalog=ingest,
            ingest_catalog_digest=ingest.digest,
            contracts=pinned,
            contract_digests={target: canonical_digest(pinned.contracts[target]) for target in pinned.contracts},
            historical_roles=roles,
        ),
    )
    assert "hist-assurance" in compiled.schema.graphs

    for layer in ("api", "e2e", "fuzz", "performance"):
        topo = classify_pinned_layer_topology_v6(renamed, _topology_spec(layer), historical_roles=roles)
        assert topo.status == "wired", (layer, topo.diagnostics)
        assert (topo.semantics_id, topo.semantics_bound) == (V6_SEMANTICS_ID, True)


def test_v6_safety_mutations() -> None:
    schema = _schema()
    roles = fixture_roles_from_schema(schema)

    zero = _strip_activation_markers(schema, "fuzz")
    zero_roles, _ = discover_historical_assurance_roles(zero)
    assert zero_roles is not None
    zero_topo = classify_pinned_layer_topology_v6(zero, _topology_spec("fuzz"), historical_roles=zero_roles)
    assert zero_topo.status == "legacy_unwired"
    assert zero_topo.semantics_bound is True

    bypass = _add_direct_codegen_bypass(schema, "api")
    bypass_roles = fixture_roles_from_schema(bypass)
    bypass_topo = classify_pinned_layer_topology_v6(bypass, _topology_spec("api"), historical_roles=bypass_roles)
    assert bypass_topo.status == "partial"
    assert any("bypass" in item for item in bypass_topo.diagnostics)

    # Unknown builtin in selection → partial.
    assurance = schema.graphs["assurance"]
    api = assurance.nodes["api"].model_copy(update={"when": "unknown_builtin(params.test_types)"})
    unknown = schema.model_copy(
        update={"graphs": {**schema.graphs, "assurance": assurance.model_copy(update={"nodes": {**assurance.nodes, "api": api}})}}
    )
    unknown_roles = fixture_roles_from_schema(unknown)
    unknown_topo = classify_pinned_layer_topology_v6(unknown, _topology_spec("api"), historical_roles=unknown_roles)
    assert unknown_topo.status == "partial"
    assert any(
        "unknown" in item or "invalid expression" in item or "allow-list" in item
        for item in unknown_topo.diagnostics
    )

    # Duplicate mechanical role → discovery fails closed / incomplete → partial.
    cycle = schema.graphs["api-plan-cycle"]
    mechanical = cycle.nodes["mechanical-plan-checks"]
    dup_nodes = {**cycle.nodes, "mechanical-plan-checks-dup": mechanical}
    dup = schema.model_copy(
        update={"graphs": {**schema.graphs, "api-plan-cycle": cycle.model_copy(update={"nodes": dup_nodes})}}
    )
    dup_roles, dup_issues = discover_historical_assurance_roles(dup)
    assert any(issue.code == "duplicate_role" for issue in dup_issues)
    assert dup_roles is None or "api" not in {item.layer for item in dup_roles.layers}
    if dup_roles is not None:
        dup_topo = classify_pinned_layer_topology_v6(dup, _topology_spec("api"), historical_roles=dup_roles)
        assert dup_topo.status == "partial"

    # Equivalent commutative predicate reorder remains wired under v6.
    gate = schema.gates["api-plan-review-gate"]
    pass_rule = next(rule for rule in gate.rules if rule.field == "pass_when")
    reordered = pass_rule.model_copy(update={"expr": reorder_commutative_and(pass_rule.expr)})
    rules = [reordered if rule.field == "pass_when" else rule for rule in gate.rules]
    equiv = schema.model_copy(
        update={"gates": {**schema.gates, "api-plan-review-gate": gate.model_copy(update={"rules": rules})}}
    )
    equiv_roles = fixture_roles_from_schema(equiv)
    equiv_topo = classify_pinned_layer_topology_v6(equiv, _topology_spec("api"), historical_roles=equiv_roles)
    assert equiv_topo.status == "wired"

    # partial remains reportable via historical compile (not a current compile gate).
    contracts = load_execution_contracts(Path.cwd())
    ingest = validate_catalog_runtime()
    current_targets = {
        node.uses
        for graph in bypass.graphs.values()
        for node in graph.nodes.values()
        if not node.uses.startswith("graph:")
    }
    from assurance_agent.workflow.graph.compiler import canonical_digest

    pinned = ExecutionContractCatalog(
        contracts={target: contracts.contracts[target] for target in current_targets if target in contracts.contracts}
    )
    compiled = compile_historical_workflow(
        bypass,
        context=HistoricalCompileContext(
            ingest_catalog=ingest,
            ingest_catalog_digest=ingest.digest,
            contracts=pinned,
            contract_digests={target: canonical_digest(pinned.contracts[target]) for target in pinned.contracts},
            historical_roles=bypass_roles,
        ),
    )
    assert compiled.digest
    assert roles.canonical_digest


def test_v6_not_used_for_frozen_display_alias() -> None:
    from assurance_agent.workflow.graph.replay_schema import classify_pinned_layer_topology

    schema = _schema()
    topo = classify_pinned_layer_topology(schema, _topology_spec("api"))
    assert (topo.semantics_id, topo.semantics_bound) == ("legacy_v5_unbound", False)


def test_v6_unbounded_selection_param_is_partial() -> None:
    schema = _schema()
    assurance = schema.graphs["assurance"]
    when = assurance.nodes["api"].when or ""
    mutated_when = f"({when}) and params.retro_id == 'x'"
    api = assurance.nodes["api"].model_copy(update={"when": mutated_when})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(update={"nodes": {**assurance.nodes, "api": api}}),
            }
        }
    )
    roles = fixture_roles_from_schema(mutated)
    topo = classify_pinned_layer_topology_v6(mutated, _topology_spec("api"), historical_roles=roles)
    assert topo.status == "partial"
    assert any("unbounded_param_domain" in item and "retro_id" in item for item in topo.diagnostics)


def test_v6_unaudited_remediation_return_is_partial() -> None:
    schema = _schema()
    cycle = schema.graphs["api-plan-cycle"]
    routes = [
        route
        if route.from_ != "knowledge-remediation"
        else route.model_copy(update={"cases": {**route.cases, "fix_and_proceed": "review"}})
        for route in cycle.routes
    ]
    mutated = schema.model_copy(
        update={"graphs": {**schema.graphs, "api-plan-cycle": cycle.model_copy(update={"routes": routes})}}
    )
    roles = fixture_roles_from_schema(mutated)
    topo = classify_pinned_layer_topology_v6(mutated, _topology_spec("api"), historical_roles=roles)
    assert topo.status == "partial"
    assert any("unaudited_remediation_return" in item for item in topo.diagnostics)


def test_load_pinned_fails_closed_on_missing_unique_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.graph.compiler import PinnedDefinitionRequest
    from assurance_agent.workflow.graph.definition_pinning import (
        PinnedDefinitionError,
        load_pinned_execution_definition,
    )
    from assurance_agent.workflow.graph import historical_roles as roles_mod
    from assurance_agent.workflow.graph.historical_roles import HistoricalRoleDiscoveryIssue
    from tests.unit.workflow.graph.test_replay_binding import _build_fixture

    fixture = _build_fixture(tmp_path, include_e2e=False)
    request = PinnedDefinitionRequest(
        graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
        ingest_catalog_digest=fixture.compiled.ingest_catalog_digest,  # type: ignore[attr-defined]
        contract_digests=tuple(sorted(fixture.compiled.contract_digests.items())),  # type: ignore[attr-defined]
        event_schema_version=4,
        gate_semantics_digest=fixture.binding.gate_semantics_digest,  # type: ignore[attr-defined]
        assurance_profile_digest=fixture.binding.assurance_profile_digest,  # type: ignore[attr-defined]
    )
    original = roles_mod.discover_historical_assurance_roles

    def discover_with_missing_role(schema):  # type: ignore[no-untyped-def]
        roles, issues = original(schema)
        assert roles is not None
        injected = HistoricalRoleDiscoveryIssue(
            code="missing_unique_role",
            layer="api",
            owner="applicability",
            locator="graph:api-plan-cycle",
            detail="expected exactly one applicability, found 0",
        )
        return roles, (*issues, injected)

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.definition_pinning.discover_historical_assurance_roles",
        discover_with_missing_role,
    )
    with pytest.raises(PinnedDefinitionError) as raised:
        load_pinned_execution_definition(fixture.change_dir, request)
    assert raised.value.reason_code == "pinned_historical_roles_invalid"
    assert "missing_unique_role" in raised.value.message
