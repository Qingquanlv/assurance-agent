"""打包 schema 与打包 contracts 必须能一起编译——新增节点忘了写 contract 时在这里炸。"""

from copy import deepcopy
from pathlib import Path

import pytest

from assurance_agent.verification.profiles import get_layer_assurance_profile, iter_layer_assurance_profiles
from assurance_agent.workflow.graph.compiler import (
    CompileError,
    compile_historical_workflow,
    compile_packaged_workflow,
    compile_workflow,
    HistoricalCompileContext,
)
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.replay_schema import (
    classify_pinned_layer_topology,
    LayerTopologySpec,
    validate_current_assurance_activation,
    validate_wired_profile_topology,
)
from assurance_agent.workflow.graph.schema_v2 import (
    load_workflow_v2,
    load_workflow_v2_with_origin,
    parse_workflow_v2,
)
from assurance_agent.workflow.orchestration.schema import derive_alias

_LAYER_PLAN_NODE = {
    "api": ("api-branch", "plan"),
    "e2e": ("e2e-branch", "plan"),
    "fuzz": ("fuzz-branch", "plan"),
    "performance": ("performance-branch", "plan"),
}

_WIRED_LAYERS = ("api", "e2e", "fuzz", "performance")


def _topology_spec(layer: str) -> LayerTopologySpec:
    profile = get_layer_assurance_profile(layer)
    return LayerTopologySpec(
        layer=profile.layer,
        plan_artifacts=profile.plan_artifacts,
        review_artifact=profile.review_artifact,
        review_alias=profile.review_alias,
        checks_artifact=profile.checks_artifact,
        gate_id=profile.gate_id,
    )


def test_packaged_schema_compiles_against_packaged_contracts() -> None:
    loaded = load_workflow_v2_with_origin(Path.cwd())
    assert loaded.origin == "packaged"
    compiled = compile_packaged_workflow(loaded.schema, contracts=load_execution_contracts(Path.cwd()))
    assert compiled.digest


def test_every_non_graph_target_has_a_contract() -> None:
    schema = load_workflow_v2(Path.cwd())
    catalog = load_execution_contracts(Path.cwd())
    missing = {
        node.uses
        for graph in schema.graphs.values()
        for node in graph.nodes.values()
        if not node.uses.startswith("graph:") and node.uses not in catalog.contracts
    }
    assert missing == set()


@pytest.mark.parametrize("layer", _WIRED_LAYERS)
def test_wired_plan_cycle_topology_matches_profile(layer: str) -> None:
    schema = load_workflow_v2(Path.cwd())
    profile = get_layer_assurance_profile(layer)
    cycle_name = f"{layer}-plan-cycle"
    cycle = schema.graphs[cycle_name]
    if layer in {"api", "e2e"}:
        assert validate_wired_profile_topology(schema, profile) == ()
    assert classify_pinned_layer_topology(schema, _topology_spec(layer)).status == "wired"
    assert "applicability" in cycle.nodes
    assert cycle.nodes["applicability"].uses == "operation:derive-plan-layer-applicability"
    assert cycle.nodes["applicability"].with_.get("layer") == layer
    assert "mechanical-plan-checks" not in cycle.nodes
    reviewer = cycle.nodes["review"]
    assert f"change:{profile.checks_artifact}" in reviewer.outputs
    assert cycle.nodes["review-gate"].uses == "builtin:gate"
    assert cycle.nodes["review-gate"].with_ == {"gate": profile.gate_id}
    assert cycle.nodes["review"].gate is None
    edges = {(edge.from_, edge.to) for edge in cycle.edges}
    assert ("START", "applicability") in edges
    assert ("review", "review-gate") in edges
    applicability_route = next(route for route in cycle.routes if route.from_ == "applicability")
    assert applicability_route.cases == {"true": "review", "false": "review-gate"}
    gate_route = next(route for route in cycle.routes if route.from_ == "review-gate")
    assert gate_route.select == "plan_review_route('review-gate')"
    assert gate_route.cases["pass"] == "END"
    assert gate_route.cases["skip"] == "END"


def test_knowledge_remediation_refreshes_plan_check_evidence() -> None:
    for layer in _WIRED_LAYERS:
        cycle = load_workflow_v2(Path.cwd()).graphs[f"{layer}-plan-cycle"]
        remediation_route = next(route for route in cycle.routes if route.from_ == "knowledge-remediation")
        assert remediation_route.cases["fix_and_proceed"] == "review"


def test_codegen_precheck_routes_skip_to_end() -> None:
    for layer in _WIRED_LAYERS:
        branch = load_workflow_v2(Path.cwd()).graphs[f"{layer}-branch"]
        route = next(route for route in branch.routes if route.from_ == "codegen-precheck")
        assert route.cases.get("skip") == "END"


def test_packaged_schema_rejects_forbidden_plan_gate_dependency() -> None:
    schema = load_workflow_v2(Path.cwd())
    profile = get_layer_assurance_profile("api")
    gate = deepcopy(schema.gates[profile.gate_id])
    gate.rules[0].expr = "node('review-gate').status == 'succeeded'"
    mutated = schema.model_copy(update={"gates": {**schema.gates, profile.gate_id: gate}})
    with pytest.raises(CompileError, match="disallowed builtin 'node'|packaged assurance activation failed"):
        compile_packaged_workflow(mutated, load_execution_contracts(Path.cwd()))


def test_layer_assurance_profiles_match_the_packaged_schema() -> None:
    schema = load_workflow_v2(Path.cwd())

    for profile in iter_layer_assurance_profiles():
        assert profile.gate_id in schema.gates, f"{profile.layer}: unknown gate {profile.gate_id!r}"

        graph_name, node_name = _LAYER_PLAN_NODE[profile.layer]
        plan_node = schema.graphs[graph_name].nodes[node_name]
        produced = {output.removeprefix("change:") for output in plan_node.outputs}
        missing = set(profile.plan_artifacts) - produced
        assert not missing, f"{profile.layer}: plan node does not produce {sorted(missing)}"


def test_current_assurance_activation_accepts_packaged_four_layers() -> None:
    schema = load_workflow_v2(Path.cwd())
    assert validate_current_assurance_activation(schema) == ()
    compile_packaged_workflow(schema, contracts=load_execution_contracts(Path.cwd()))


def test_strict_current_conformance_is_compile_gate_after_activation() -> None:
    """Packaged compile requires assurance + healing conformance."""
    from assurance_agent.workflow.graph.assurance_conformance import (
        find_current_assurance_conformance_issues,
    )
    from assurance_agent.workflow.graph.healing_conformance import (
        find_current_healing_conformance_issues,
    )

    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    assert validate_current_assurance_activation(schema) == ()
    assert find_current_assurance_conformance_issues(schema) == ()
    assert find_current_healing_conformance_issues(schema, contracts) == ()
    compile_packaged_workflow(schema, contracts=contracts)
    with pytest.raises(CompileError, match="requires a non-null contracts catalog"):
        compile_packaged_workflow(schema, contracts=None)


def test_core_compile_still_accepts_minimal_non_assurance_graph() -> None:
    schema = parse_workflow_v2(
        """
name: minimal
params:
  run_mode: {type: str, default: full}
entrypoints:
  full: {graph: g}
graphs:
  g:
    max_supersteps: 1
    nodes:
      done:
        uses: operation:stop
        with: {reason: ok}
    edges:
      - {from: START, to: done}
      - {from: done, to: END}
"""
    )
    compiled = compile_workflow(schema)
    assert compiled.digest


def test_historical_compile_still_accepts_legacy_specialty_fixture() -> None:
    from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog

    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    ingest = validate_catalog_runtime()
    current = compile_packaged_workflow(schema, contracts)
    pinned_contracts = ExecutionContractCatalog(
        contracts={
            target: contracts.contracts[target]
            for target in current.contract_digests
            if target in contracts.contracts
        }
    )
    from assurance_agent.workflow.graph.historical_roles import fixture_roles_from_schema

    historical = compile_historical_workflow(
        schema,
        context=HistoricalCompileContext(
            ingest_catalog=ingest,
            ingest_catalog_digest=ingest.digest,
            contracts=pinned_contracts,
            contract_digests=dict(current.contract_digests),
            historical_roles=fixture_roles_from_schema(schema),
        ),
    )
    assert historical.digest == current.digest


def test_wired_plan_gates_read_canonical_aliases() -> None:
    schema = load_workflow_v2(Path.cwd())
    for layer in _WIRED_LAYERS:
        profile = get_layer_assurance_profile(layer)
        gate = schema.gates[profile.gate_id]
        reads_by_path = {entry.path: entry.alias for entry in gate.reads}
        assert reads_by_path[profile.review_artifact] == profile.review_alias
        assert reads_by_path[profile.checks_artifact] == derive_alias(profile.checks_artifact)
        assert reads_by_path["repo:.aa/data-knowledge.yaml"] == "data_knowledge"
