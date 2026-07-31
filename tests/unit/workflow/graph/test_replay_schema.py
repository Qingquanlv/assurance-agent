"""Replayable assurance schema dependency and topology guards."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.replay_schema import (
    WIRED_REPLAY_LAYERS,
    validate_params_only_expression,
    validate_replayable_assurance_schema,
    validate_replayable_plan_gate,
    validate_wired_profile_topology,
)
from assurance_agent.workflow.graph.schema_v2 import (
    EdgeDef,
    EntrypointDef,
    GraphDef,
    InterruptDef,
    NodeDef,
    ParamDef,
    RouteDef,
    WorkflowSchemaV2,
    load_workflow_v2,
)
from assurance_agent.workflow.orchestration.schema import GateDef, GateRule, ReadEntry, Verdict, derive_alias

SCHEMA_REL = Path("assurance_agent/_resources/schemas/workflow-schema.yaml")
BRANCH_NODES = ("api", "e2e", "fuzz", "performance")
PARAM_NAMES = frozenset(
    {
        "run_mode",
        "test_types",
        "run_tests",
        "auto_archive",
        "max_healing_attempts",
        "force_continue",
        "max_plan_fix_attempts",
    }
)

APPLICABILITY_OP = "operation:derive-plan-layer-applicability"
MECHANICAL_OP = "operation:verify-plan-mechanical"


def _node_def(uses: str, *, with_params: dict[str, object] | None = None, **fields: object) -> NodeDef:
    payload: dict[str, object] = {"uses": uses, **fields}
    if with_params is not None:
        payload["with"] = with_params
    return NodeDef.model_validate(payload)


def _branch_when(schema, node_id: str) -> str:
    return " ".join((schema.graphs["assurance"].nodes[node_id].when or "").split())


def _checks_alias(layer: str) -> str:
    profile = get_layer_assurance_profile(layer)
    return derive_alias(profile.checks_artifact)


def _compliant_plan_gate(*, layer: str) -> GateDef:
    profile = get_layer_assurance_profile(layer)
    checks_alias = _checks_alias(layer)
    return GateDef(
        id=profile.gate_id,
        reads=[
            ReadEntry(path=profile.review_artifact, alias=profile.review_alias),
            ReadEntry(path=profile.checks_artifact, alias=checks_alias),
            ReadEntry(path="repo:.aa/data-knowledge.yaml", alias="data_knowledge"),
        ],
        rules=[
            GateRule(
                field="stop_when",
                verdict=Verdict.STOP,
                expr=(
                    f"plan_assurance_state({checks_alias}, {profile.review_alias}, "
                    f"data_knowledge, '{layer}') == 'invalid'"
                ),
            ),
            GateRule(
                field="skip_when",
                verdict=Verdict.SKIP,
                expr=(
                    f"plan_assurance_state({checks_alias}, {profile.review_alias}, "
                    f"data_knowledge, '{layer}') == 'not_applicable'"
                ),
            ),
            GateRule(
                field="needs_fix_when",
                verdict=Verdict.NEEDS_FIX,
                expr=(
                    f"{profile.review_alias}.decision == 'needs_fix' "
                    f"and {profile.review_alias}.auto_fix_allowed == true"
                ),
            ),
            # Task 11 must preserve explicit-reject-before-human-review when migrating packaged gates.
            GateRule(
                field="needs_human_review_when",
                verdict=Verdict.NEEDS_HUMAN_REVIEW,
                expr=(
                    f"{profile.review_alias}.decision != 'reject' and ("
                    f"not capabilities_present({profile.review_alias}, data_knowledge) "
                    f"or check_failed({checks_alias}, 'capability_keys')"
                    f")"
                ),
            ),
            GateRule(
                field="reject_when",
                verdict=Verdict.REJECT,
                expr=f"{profile.review_alias}.decision == 'reject'",
            ),
            GateRule(
                field="pass_when",
                verdict=Verdict.PASS,
                expr=(
                    f"{profile.review_alias}.decision == 'pass' "
                    f"and capabilities_present({profile.review_alias}, data_knowledge) "
                    f"and policy.coverage_floor.risk_high > 0"
                ),
            ),
        ],
    )


def _codegen_gate(*, layer: str) -> GateDef:
    profile = get_layer_assurance_profile(layer)
    checks_alias = _checks_alias(layer)
    plan_gate = profile.gate_id
    return GateDef(
        id=f"{layer}-codegen-precondition-gate",
        reads=[ReadEntry(path=profile.checks_artifact, alias=checks_alias)],
        rules=[
            GateRule(
                field="pass_when",
                verdict=Verdict.PASS,
                expr=(
                    f"node('{layer}-plan-cycle').status == 'succeeded' "
                    f"and gate('{plan_gate}').verdict == 'pass' "
                    f"and file_exists('repo:.aa/data-knowledge.yaml')"
                ),
            ),
        ],
    )


def _plan_cycle_graph(*, layer: str, mutate: dict | None = None) -> GraphDef:
    profile = get_layer_assurance_profile(layer)
    reviewer_skill = f"skill:aa-{layer}-plan-reviewer"
    fixer_skill = f"skill:aa-{layer}-plan-fixer"
    graph = GraphDef(
        max_supersteps=20,
        nodes={
            "applicability": _node_def(
                APPLICABILITY_OP,
                with_params={"layer": layer},
            ),
            "review": NodeDef(
                uses=reviewer_skill,
                agent="aa-reviewer",
                outputs=[
                    f"change:{profile.review_artifact}",
                    f"change:{profile.review_artifact.replace('.json', '-summary.md')}",
                ],
            ),
            "mechanical-plan-checks": _node_def(
                MECHANICAL_OP,
                with_params={"layer": layer, "require_review": True},
                outputs=[f"change:{profile.checks_artifact}"],
            ),
            "review-gate": _node_def(
                "builtin:gate",
                with_params={"gate": profile.gate_id},
            ),
            "fix": NodeDef(
                uses=fixer_skill,
                agent="aa-doc-author",
                outputs=[f"change:{profile.review_artifact.replace('.json', '-apply-summary.md')}"],
            ),
            "human-review": NodeDef(
                uses="builtin:interrupt",
                interrupt=InterruptDef(
                    reason=f"{layer} plan review requires human review",
                    checkpoint=profile.gate_id,
                    bind="audited_gate_read",
                    actions=["fix_and_proceed", "accept_risk", "stop"],
                ),
            ),
            "knowledge-remediation": NodeDef(
                uses="builtin:interrupt",
                interrupt=InterruptDef(
                    reason=f"{layer} plan review requires L1 remediation",
                    checkpoint=profile.gate_id,
                    bind="audited_gate_read",
                    actions=["fix_and_proceed", "accept_risk", "stop"],
                ),
            ),
            "exhausted": _node_def(
                "operation:stop",
                with_params={"reason": f"{layer} plan fix attempts exhausted"},
            ),
        },
        edges=[
            EdgeDef(**{"from": "START", "to": "applicability"}),
            EdgeDef(**{"from": "review", "to": "mechanical-plan-checks"}),
            EdgeDef(**{"from": "mechanical-plan-checks", "to": "review-gate"}),
            EdgeDef(**{"from": "fix", "to": "review"}),
            EdgeDef(**{"from": "exhausted", "to": "STOP"}),
        ],
        routes=[
            RouteDef(
                **{
                    "from": "applicability",
                    "select": "node('applicability').value.applicable",
                    "cases": {"true": "review", "false": "mechanical-plan-checks"},
                    "default": "STOP",
                }
            ),
            RouteDef(
                **{
                    "from": "review-gate",
                    "select": "plan_review_route('review-gate')",
                    "cases": {
                        "pass": "END",
                        "skip": "END",
                        "needs_fix": "fix",
                        "knowledge_remediation": "knowledge-remediation",
                        "needs_human_review": "human-review",
                        "reject": "STOP",
                        "stop": "STOP",
                    },
                    "default": "STOP",
                }
            ),
            RouteDef(
                **{
                    "from": "human-review",
                    "select": "resume.action",
                    "cases": {"fix_and_proceed": "fix", "accept_risk": "END", "stop": "STOP"},
                    "default": "STOP",
                }
            ),
            RouteDef(
                **{
                    "from": "knowledge-remediation",
                    "select": "resume.action",
                    "cases": {
                        "fix_and_proceed": "mechanical-plan-checks",
                        "accept_risk": "END",
                        "stop": "STOP",
                    },
                    "default": "STOP",
                }
            ),
        ],
    )
    if mutate:
        for key, value in mutate.items():
            if key == "nodes":
                for node_id, node in value.items():
                    graph.nodes[node_id] = node
            elif key == "edges":
                graph.edges = value
            elif key == "routes":
                graph.routes = value
            elif key == "drop_nodes":
                for node_id in value:
                    del graph.nodes[node_id]
    return graph


def _branch_graph(*, layer: str) -> GraphDef:
    return GraphDef(
        max_supersteps=30,
        nodes={
            "review-cycle": NodeDef(
                uses=f"graph:{layer}-plan-cycle",
            ),
            "codegen-precheck": _node_def(
                "builtin:gate",
                with_params={"gate": f"{layer}-codegen-precondition-gate"},
            ),
            "codegen": NodeDef(
                uses=f"skill:aa-{layer}-codegen",
                agent="aa-test-author",
                when="params.run_mode in ['full','codegen-only']",
                outputs=[f"change:codegen/{layer}-codegen-summary.md"],
            ),
        },
        edges=[
            EdgeDef(**{"from": "START", "to": "review-cycle"}),
            EdgeDef(**{"from": "review-cycle", "to": "codegen-precheck"}),
        ],
        routes=[
            RouteDef(
                **{
                    "from": "codegen-precheck",
                    "select": "node('codegen-precheck').gate.verdict",
                    "cases": {"pass": "codegen", "skip": "END", "stop": "STOP"},
                    "default": "STOP",
                }
            ),
        ],
    )


def _minimal_replay_schema(*, layer: str = "api") -> WorkflowSchemaV2:
    _ = layer
    gates: dict[str, GateDef] = {}
    graphs: dict[str, GraphDef] = {
        "assurance": GraphDef(
            max_supersteps=10,
            nodes={
                node_id: NodeDef(
                    uses=f"graph:{node_id}-branch",
                    when=(
                        f"'{node_id}' in params.test_types "
                        f"and params.run_mode in ['full','{node_id}-only','plan-only']"
                        if node_id in WIRED_REPLAY_LAYERS
                        else f"'{node_id}' in params.test_types"
                    ),
                )
                for node_id in BRANCH_NODES
            },
            edges=[EdgeDef(**{"from": "START", "to": node_id}) for node_id in BRANCH_NODES],
        ),
    }
    for wired_layer in sorted(WIRED_REPLAY_LAYERS):
        profile = get_layer_assurance_profile(wired_layer)
        gates[profile.gate_id] = _compliant_plan_gate(layer=wired_layer)
        gates[f"{wired_layer}-codegen-precondition-gate"] = _codegen_gate(layer=wired_layer)
        graphs[f"{wired_layer}-branch"] = _branch_graph(layer=wired_layer)
        graphs[f"{wired_layer}-plan-cycle"] = _plan_cycle_graph(layer=wired_layer)
    return WorkflowSchemaV2(
        schema_version="2",
        name="replay-test",
        params={name: ParamDef(type="str") for name in PARAM_NAMES},
        entrypoints={"execute": EntrypointDef(graph="assurance")},
        graphs=graphs,
        gates=gates,
    )


@pytest.fixture(scope="module")
def packaged_schema() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd(), SCHEMA_REL)


@pytest.mark.parametrize("node_id", BRANCH_NODES)
def test_packaged_assurance_branch_predicates_are_params_only(
    packaged_schema: WorkflowSchemaV2, node_id: str
) -> None:
    when = _branch_when(packaged_schema, node_id)
    assert validate_params_only_expression(when, PARAM_NAMES) == ()


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        ("api_plan_review.decision == 'pass'", "disallowed identifier 'api_plan_review'"),
        ("state.phases['api'] == 'done'", "disallowed identifier 'state'"),
        ("node('api').status == 'succeeded'", "disallowed builtin 'node'"),
        ("gate('api-plan-review-gate').verdict == 'pass'", "disallowed builtin 'gate'"),
        ("file_exists('repo:.aa/data-knowledge.yaml')", "disallowed builtin 'file_exists'"),
    ],
)
def test_params_only_expression_rejects_forbidden_dependencies(
    packaged_schema: WorkflowSchemaV2, mutation: str, expected_fragment: str
) -> None:
    base = _branch_when(packaged_schema, "api")
    expr = f"({base}) and ({mutation})"
    locator = "expr:test"
    errors = validate_params_only_expression(expr, PARAM_NAMES, locator=locator)
    assert len(errors) == 1
    assert errors[0].startswith(f"{locator}:")
    assert expected_fragment in errors[0]


def test_params_only_corpus_uses_run_mode_and_test_types(packaged_schema: WorkflowSchemaV2) -> None:
    for node_id in BRANCH_NODES:
        when = _branch_when(packaged_schema, node_id)
        assert "params.run_mode" in when
        assert "params.test_types" in when


def test_compliant_plan_gate_accepts_replayable_dependencies() -> None:
    gate = _compliant_plan_gate(layer="api")
    assert validate_replayable_plan_gate(gate) == ()


@pytest.mark.parametrize(
    ("expr_fragment", "expected_fragment"),
    [
        ("state.change_id == 'x'", "disallowed identifier 'state'"),
        ("node('review').status == 'succeeded'", "disallowed builtin 'node'"),
        ("gate('other-gate').verdict == 'pass'", "disallowed builtin 'gate'"),
        ("file_exists('repo:.aa/data-knowledge.yaml')", "disallowed builtin 'file_exists'"),
        ("unknown_symbol == true", "disallowed identifier 'unknown_symbol'"),
    ],
)
def test_replayable_plan_gate_rejects_forbidden_dependencies(
    expr_fragment: str, expected_fragment: str
) -> None:
    gate = _compliant_plan_gate(layer="api")
    rules = list(gate.rules)
    rules[0] = GateRule(field="stop_when", verdict=Verdict.STOP, expr=expr_fragment)
    mutated = gate.model_copy(update={"rules": rules})
    errors = validate_replayable_plan_gate(mutated)
    assert len(errors) == 1
    assert errors[0].startswith(f"gate:{gate.id}:stop_when:")
    assert expected_fragment in errors[0]


def test_codegen_gate_uses_node_and_gate_but_is_outside_plan_gate_validation() -> None:
    gate = _codegen_gate(layer="api")
    # The helper itself flags forbidden deps — codegen gates are excluded at schema level.
    errors = validate_replayable_plan_gate(gate)
    assert any("disallowed builtin 'node'" in err for err in errors)
    schema = _minimal_replay_schema(layer="api")
    assert validate_replayable_assurance_schema(schema) == ()


def test_wired_profile_topology_accepts_minimal_compliant_graph() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    assert validate_wired_profile_topology(schema, profile) == ()


def _with_plan_cycle(schema: WorkflowSchemaV2, *, layer: str, graph: GraphDef) -> WorkflowSchemaV2:
    graphs = dict(schema.graphs)
    graphs[f"{layer}-plan-cycle"] = graph
    return schema.model_copy(update={"graphs": graphs})


@pytest.mark.parametrize(
    ("mutator", "expected_fragment"),
    [
        (
            lambda graph: graph.model_copy(
                update={"nodes": {k: v for k, v in graph.nodes.items() if k != "applicability"}}
            ),
            "missing applicability operation",
        ),
        (
            lambda graph: graph.model_copy(
                update={"nodes": {k: v for k, v in graph.nodes.items() if k != "mechanical-plan-checks"}}
            ),
            "missing reviewed mechanical producer",
        ),
        (
            lambda graph: graph.model_copy(
                update={"nodes": {k: v for k, v in graph.nodes.items() if k != "review-gate"}}
            ),
            "missing explicit gate owner",
        ),
    ],
)
def test_wired_profile_topology_rejects_missing_required_nodes(mutator, expected_fragment: str) -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    mutated = mutator(schema.graphs["api-plan-cycle"])
    schema = _with_plan_cycle(schema, layer="api", graph=mutated)
    errors = validate_wired_profile_topology(schema, profile)
    assert any(expected_fragment in err for err in errors)


def test_wired_profile_topology_rejects_swapped_applicability_paths() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    routes = [
        route
        if route.from_ != "applicability"
        else route.model_copy(update={"cases": {"true": "mechanical-plan-checks", "false": "review"}})
        for route in cycle.routes
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"routes": routes}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any("applicable path must reach review before mechanical" in err for err in errors)


def test_wired_profile_topology_rejects_inapplicable_path_bypassing_mechanical() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    routes = [
        route
        if route.from_ != "applicability"
        else route.model_copy(update={"cases": {"true": "review", "false": "END"}})
        for route in cycle.routes
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"routes": routes}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any("inapplicable path must reach mechanical producer" in err for err in errors)


def test_wired_profile_topology_rejects_review_after_mechanical_on_applicable_path() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    edges = [
        edge for edge in cycle.edges if not (edge.from_ == "review" and edge.to == "mechanical-plan-checks")
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"edges": edges}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any("review must precede mechanical on applicable path" in err for err in errors)


def test_wired_profile_topology_rejects_gate_before_mechanical() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    edges = [
        EdgeDef(**{"from": "START", "to": "applicability"}),
        EdgeDef(**{"from": "review", "to": "review-gate"}),
        EdgeDef(**{"from": "fix", "to": "review"}),
        EdgeDef(**{"from": "exhausted", "to": "STOP"}),
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"edges": edges}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any("explicit gate must follow mechanical producer" in err for err in errors)


def test_wired_profile_topology_rejects_wrong_knowledge_remediation_freshness() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    routes = [
        route
        if route.from_ != "knowledge-remediation"
        else route.model_copy(
            update={"cases": {"fix_and_proceed": "review", "accept_risk": "END", "stop": "STOP"}}
        )
        for route in cycle.routes
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"routes": routes}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any(
        "knowledge remediation fix_and_proceed must re-enter at mechanical producer" in err for err in errors
    )


def test_wired_profile_topology_rejects_fix_skipping_review() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    cycle = schema.graphs["api-plan-cycle"]
    edges = [
        edge
        if not (edge.from_ == "fix" and edge.to == "review")
        else EdgeDef(**{"from": "fix", "to": "mechanical-plan-checks"})
        for edge in cycle.edges
    ]
    schema = _with_plan_cycle(schema, layer="api", graph=cycle.model_copy(update={"edges": edges}))
    errors = validate_wired_profile_topology(schema, profile)
    assert any("fix must re-enter at review" in err for err in errors)


def test_wired_profile_topology_rejects_attached_reviewer_gate() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    review = schema.graphs["api-plan-cycle"].nodes["review"].model_copy(update={"gate": profile.gate_id})
    nodes = dict(schema.graphs["api-plan-cycle"].nodes)
    nodes["review"] = review
    cycle = schema.graphs["api-plan-cycle"].model_copy(update={"nodes": nodes})
    graphs = dict(schema.graphs)
    graphs["api-plan-cycle"] = cycle
    schema = schema.model_copy(update={"graphs": graphs})
    errors = validate_wired_profile_topology(schema, profile)
    assert any("attached reviewer gate" in err for err in errors)


def test_wired_profile_topology_rejects_wrong_gate_reads() -> None:
    schema = _minimal_replay_schema(layer="api")
    profile = get_layer_assurance_profile("api")
    gate = deepcopy(schema.gates[profile.gate_id])
    gate.reads = [ReadEntry(path="review/wrong.json", alias="wrong_review")]
    gates = dict(schema.gates)
    gates[profile.gate_id] = gate
    schema = schema.model_copy(update={"gates": gates})
    errors = validate_wired_profile_topology(schema, profile)
    assert any("gate reads must include review artifact" in err for err in errors)


def test_validate_replayable_assurance_schema_composes_branch_and_topology_guards() -> None:
    schema = _minimal_replay_schema(layer="e2e")
    assert validate_replayable_assurance_schema(schema) == ()


def test_packaged_schema_passes_replay_topology_guard(packaged_schema: WorkflowSchemaV2) -> None:
    assert validate_replayable_assurance_schema(packaged_schema) == ()


def test_validate_replayable_assurance_schema_rejects_noncompliant_branch_predicate() -> None:
    schema = _minimal_replay_schema(layer="api")
    nodes = dict(schema.graphs["assurance"].nodes)
    api_node = nodes["api"].model_copy(update={"when": "gate('api-plan-review-gate').verdict == 'pass'"})
    nodes["api"] = api_node
    assurance = schema.graphs["assurance"].model_copy(update={"nodes": nodes})
    graphs = dict(schema.graphs)
    graphs["assurance"] = assurance
    schema = schema.model_copy(update={"graphs": graphs})
    errors = validate_replayable_assurance_schema(schema)
    assert any(err.startswith("graph:assurance.nodes.api.when:") for err in errors)


def test_wired_replay_layers_constant() -> None:
    assert WIRED_REPLAY_LAYERS == frozenset({"api", "e2e"})
