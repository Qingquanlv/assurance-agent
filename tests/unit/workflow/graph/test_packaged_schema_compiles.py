"""打包 schema 与打包 contracts 必须能一起编译——新增节点忘了写 contract 时在这里炸。"""

from pathlib import Path

from assurance_agent.verification.profiles import iter_layer_assurance_profiles
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2

_LAYER_PLAN_NODE = {
    "api": ("api-branch", "plan"),
    "e2e": ("e2e-branch", "plan"),
    "fuzz": ("fuzz-branch", "plan"),
    "performance": ("performance-branch", "plan"),
}


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


def test_plan_check_node_lives_in_the_review_cycle() -> None:
    """evidence 每轮重算：fix → checks → review，而不是只在首轮跑一次。"""
    cycle = load_workflow_v2(Path.cwd()).graphs["api-plan-cycle"]
    assert "mechanical-plan-checks" in cycle.nodes
    edges = {(edge.from_, edge.to) for edge in cycle.edges}
    assert ("START", "mechanical-plan-checks") in edges
    assert ("mechanical-plan-checks", "review") in edges
    assert ("fix", "mechanical-plan-checks") in edges
    assert ("fix", "review") not in edges


def test_knowledge_remediation_refreshes_plan_check_evidence() -> None:
    cycle = load_workflow_v2(Path.cwd()).graphs["api-plan-cycle"]
    remediation_route = next(route for route in cycle.routes if route.from_ == "knowledge-remediation")
    assert remediation_route.cases["fix_and_proceed"] == "mechanical-plan-checks"


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
