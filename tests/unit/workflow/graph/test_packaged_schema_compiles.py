"""打包 schema 与打包 contracts 必须能一起编译——新增节点忘了写 contract 时在这里炸。"""

from copy import deepcopy
from pathlib import Path

import pytest

from assurance_agent.verification.profiles import get_layer_assurance_profile, iter_layer_assurance_profiles
from assurance_agent.workflow.graph.compiler import CompileError, compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.replay_schema import (
    validate_replayable_assurance_schema,
    validate_wired_profile_topology,
)
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.schema import derive_alias

_LAYER_PLAN_NODE = {
    "api": ("api-branch", "plan"),
    "e2e": ("e2e-branch", "plan"),
    "fuzz": ("fuzz-branch", "plan"),
    "performance": ("performance-branch", "plan"),
}

_WIRED_LAYERS = ("api", "e2e")


def test_packaged_schema_compiles_against_packaged_contracts() -> None:
    compiled = compile_workflow(
        load_workflow_v2(Path.cwd()),
        contracts=load_execution_contracts(Path.cwd()),
    )
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
    cycle = schema.graphs[f"{layer}-plan-cycle"]
    assert validate_wired_profile_topology(schema, profile) == ()
    assert "applicability" in cycle.nodes
    assert cycle.nodes["applicability"].uses == "operation:derive-plan-layer-applicability"
    assert cycle.nodes["applicability"].with_.get("layer") == layer
    mechanical = cycle.nodes["mechanical-plan-checks"]
    assert mechanical.uses == "operation:verify-plan-mechanical"
    assert mechanical.with_ == {"layer": layer, "require_review": True}
    assert f"change:{profile.checks_artifact}" in mechanical.outputs
    assert cycle.nodes["review-gate"].uses == "builtin:gate"
    assert cycle.nodes["review-gate"].with_ == {"gate": profile.gate_id}
    assert cycle.nodes["review"].gate is None
    edges = {(edge.from_, edge.to) for edge in cycle.edges}
    assert ("START", "applicability") in edges
    assert ("review", "mechanical-plan-checks") in edges
    assert ("mechanical-plan-checks", "review-gate") in edges
    assert ("fix", "review") in edges
    applicability_route = next(route for route in cycle.routes if route.from_ == "applicability")
    assert applicability_route.cases == {"true": "review", "false": "mechanical-plan-checks"}
    gate_route = next(route for route in cycle.routes if route.from_ == "review-gate")
    assert gate_route.select == "plan_review_route('review-gate')"
    assert gate_route.cases["pass"] == "END"
    assert gate_route.cases["skip"] == "END"


def test_knowledge_remediation_refreshes_plan_check_evidence() -> None:
    for layer in _WIRED_LAYERS:
        cycle = load_workflow_v2(Path.cwd()).graphs[f"{layer}-plan-cycle"]
        remediation_route = next(route for route in cycle.routes if route.from_ == "knowledge-remediation")
        assert remediation_route.cases["fix_and_proceed"] == "mechanical-plan-checks"


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
    with pytest.raises(CompileError, match="disallowed builtin 'node'"):
        compile_workflow(mutated, load_execution_contracts(Path.cwd()))


def test_layer_assurance_profiles_match_the_packaged_schema() -> None:
    """Every profile's gate_id and plan_artifacts must have a durable counterpart
    in workflow-schema.yaml, or the profile and the schema have silently drifted."""
    schema = load_workflow_v2(Path.cwd())

    for profile in iter_layer_assurance_profiles():
        assert profile.gate_id in schema.gates, f"{profile.layer}: unknown gate {profile.gate_id!r}"

        graph_name, node_name = _LAYER_PLAN_NODE[profile.layer]
        plan_node = schema.graphs[graph_name].nodes[node_name]
        produced = {output.removeprefix("change:") for output in plan_node.outputs}
        missing = set(profile.plan_artifacts) - produced
        assert not missing, f"{profile.layer}: plan node does not produce {sorted(missing)}"


def test_packaged_assurance_surface_passes_replay_schema_guard() -> None:
    schema = load_workflow_v2(Path.cwd())
    assert validate_replayable_assurance_schema(schema) == ()


def test_wired_plan_gates_read_canonical_aliases() -> None:
    schema = load_workflow_v2(Path.cwd())
    for layer in _WIRED_LAYERS:
        profile = get_layer_assurance_profile(layer)
        gate = schema.gates[profile.gate_id]
        reads_by_path = {entry.path: entry.alias for entry in gate.reads}
        assert reads_by_path[profile.review_artifact] == profile.review_alias
        assert reads_by_path[profile.checks_artifact] == derive_alias(profile.checks_artifact)
        assert reads_by_path["repo:.aa/data-knowledge.yaml"] == "data_knowledge"
