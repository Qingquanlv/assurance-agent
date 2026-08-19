import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine.errors import GraphEngineError
from graph_engine.graph.compiler import CompiledNode, CompiledWorkflow, CompileError, compile_workflow
from graph_engine.graph.expressions import ExpressionError, evaluate_expression
from graph_engine.graph.schema import NodeDef, parse_workflow
from graph_engine.plugin_api import (
    CandidateWriteSet,
    CapabilityRegistry,
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    ValidationContext,
    ValidationResult,
    assemble_registry,
)


async def _ping(_request, _context):
    from graph_engine.plugin_api import TaskOutcome

    return TaskOutcome.succeeded({"pong": True})


class _Provider:
    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id="toy.one",
            plugin_version="1.0.0",
            engine_api="1.0",
            task_handlers=("toy.one.ping",),
            commit_validators=("toy.one.clean",),
        )

    def bind(self, _ports: EnginePorts) -> PluginRuntime:
        return PluginRuntime(
            task_handlers={"toy.one.ping": _ping},
            commit_validators={"toy.one.clean": _Validator()},
        )


class _Validator:
    def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
        return ValidationResult(accepted=True)


@pytest.fixture
def registry() -> CapabilityRegistry:
    return assemble_registry((_Provider(),))


VALID = """
name: toy
entrypoints: {main: root}
retry: {once: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 20
    start: ping
    nodes:
      ping: {kind: task, capability: toy.one.ping, retry: once, timeout: short}
      done: {kind: end}
    edges:
      - {from: ping, to: done}
"""


def test_compile_is_deterministic(registry: CapabilityRegistry) -> None:
    workflow = parse_workflow(VALID)
    first = compile_workflow(workflow, registry)
    second = compile_workflow(workflow, registry)
    assert first.digest == second.digest
    assert first.graphs["root"].declaration_order == ("ping", "done")


def test_unknown_capability_fails_before_runtime() -> None:
    workflow = parse_workflow(VALID)
    with pytest.raises(CompileError, match="unknown capability toy.one.ping"):
        compile_workflow(workflow, CapabilityRegistry.empty())


def test_unreachable_node_is_rejected(registry: CapabilityRegistry) -> None:
    workflow = parse_workflow(
        VALID.replace(
            "      done: {kind: end}",
            "      done: {kind: end}\n      lost: {kind: end}",
        )
    )
    with pytest.raises(CompileError, match="unreachable node root/lost"):
        compile_workflow(workflow, registry)


def _raw_valid() -> dict[str, object]:
    loaded = yaml.safe_load(VALID)
    assert isinstance(loaded, dict)
    return loaded


def _parse_raw(raw: dict[str, object]):
    return parse_workflow(yaml.safe_dump(raw, sort_keys=False))


@pytest.mark.parametrize(
    ("path", "unknown"),
    [
        ((), "workflow_extra"),
        (("retry", "once"), "retry_extra"),
        (("timeout", "short"), "timeout_extra"),
        (("graphs", "root"), "graph_extra"),
        (("graphs", "root", "nodes", "ping"), "node_extra"),
        (("graphs", "root", "edges", 0), "edge_extra"),
    ],
)
def test_schema_rejects_unknown_keys(path: tuple[object, ...], unknown: str) -> None:
    raw: object = _raw_valid()
    for part in path:
        assert isinstance(raw, dict | list)
        raw = raw[part]  # type: ignore[index]
    assert isinstance(raw, dict)
    raw[unknown] = True

    with pytest.raises(ValidationError, match=unknown):
        _parse_raw(_raw_from_nested(path, raw))


def _raw_from_nested(path: tuple[object, ...], replacement: object) -> dict[str, object]:
    root = _raw_valid()
    if not path:
        assert isinstance(replacement, dict)
        return replacement
    parent: object = root
    for part in path[:-1]:
        assert isinstance(parent, dict | list)
        parent = parent[part]  # type: ignore[index]
    assert isinstance(parent, dict | list)
    parent[path[-1]] = replacement  # type: ignore[index]
    return root


def test_schema_models_are_frozen() -> None:
    workflow = parse_workflow(VALID)
    with pytest.raises(ValidationError, match="frozen"):
        workflow.name = "changed"


def test_schema_rejects_python_edge_field_name_in_yaml() -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["edges"][0] = {"from_": "ping", "to": "done"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="from"):
        _parse_raw(raw)


@pytest.mark.parametrize("maximum", [1, 10_000])
def test_graph_activation_bound_accepts_endpoints(maximum: int) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["max_activations"] = maximum  # type: ignore[index]
    assert _parse_raw(raw).graphs["root"].max_activations == maximum


@pytest.mark.parametrize("maximum", [0, 10_001])
def test_graph_activation_bound_rejects_out_of_range(maximum: int) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["max_activations"] = maximum  # type: ignore[index]
    with pytest.raises(ValidationError, match="max_activations"):
        _parse_raw(raw)


@pytest.mark.parametrize("missing", ["capability", "retry", "timeout"])
def test_task_requires_execution_references(missing: str) -> None:
    payload = {
        "kind": "task",
        "capability": "toy.one.ping",
        "retry": "once",
        "timeout": "short",
    }
    del payload[missing]
    with pytest.raises(ValidationError, match=missing):
        NodeDef.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "required"),
    [
        ({"kind": "subgraph"}, "graph"),
        ({"kind": "join"}, "join"),
        ({"kind": "gate"}, "expression"),
        ({"kind": "interrupt", "actions": ["continue"]}, "reason"),
        ({"kind": "interrupt", "reason": "choose"}, "actions"),
    ],
)
def test_node_kinds_require_their_payload(payload: dict[str, object], required: str) -> None:
    with pytest.raises(ValidationError, match=required):
        NodeDef.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "interrupt", "reason": "", "actions": ["continue"]},
        {"kind": "interrupt", "reason": "   ", "actions": ["continue"]},
        {"kind": "interrupt", "reason": "choose", "actions": [""]},
        {"kind": "interrupt", "reason": "choose", "actions": ["   "]},
        {"kind": "interrupt", "reason": "choose", "actions": ["continue", "continue"]},
    ],
)
def test_interrupt_requires_non_empty_unique_actions(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NodeDef.model_validate(payload)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("task", {"graph": "child"}),
        ("subgraph", {"capability": "toy.one.ping"}),
        ("join", {"expression": "true"}),
        ("gate", {"reason": "stop"}),
        ("interrupt", {"join": "all"}),
    ],
)
def test_node_kinds_reject_foreign_payload(kind: str, payload: dict[str, object]) -> None:
    required = {
        "task": {"capability": "toy.one.ping", "retry": "once", "timeout": "short"},
        "subgraph": {"graph": "child"},
        "join": {"join": "any"},
        "gate": {"expression": "true"},
        "interrupt": {"reason": "choose", "actions": ["continue"]},
    }[kind]
    with pytest.raises(ValidationError):
        NodeDef.model_validate({"kind": kind, **required, **payload})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability", "toy.one.ping"),
        ("graph", "child"),
        ("join", "all"),
        ("expression", "true"),
        ("reason", "done"),
        ("actions", ["continue"]),
        ("input", {"route": "left"}),
        ("retry", "once"),
        ("timeout", "short"),
        ("resources", {"reads": ["src"]}),
        ("validators", ["toy.one.clean"]),
    ],
)
def test_end_rejects_non_end_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        NodeDef.model_validate({"kind": "end", field: value})


def test_policy_numeric_boundaries_are_positive() -> None:
    raw = _raw_valid()
    raw["retry"]["once"]["max_attempts"] = 0  # type: ignore[index]
    with pytest.raises(ValidationError, match="max_attempts"):
        _parse_raw(raw)

    raw = _raw_valid()
    raw["timeout"]["short"]["run_seconds"] = 0  # type: ignore[index]
    with pytest.raises(ValidationError, match="run_seconds"):
        _parse_raw(raw)


def test_expression_reads_dictionary_style_names() -> None:
    assert evaluate_expression('input.route == "left"', {"input": {"route": "left"}}) is True


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("true and not false", True),
        ("null == null", True),
        ("input.value != 3", True),
        ("input.value < 3", True),
        ("input.value <= 2", True),
        ("input.value > 1", True),
        ("input.value >= 2", True),
        ("input.route in input.allowed", True),
        ("false or true", True),
    ],
)
def test_expression_supports_the_closed_operator_set(expression: str, expected: bool) -> None:
    scope = {"input": {"value": 2, "route": "left", "allowed": ["left", "right"]}}
    assert evaluate_expression(expression, scope) is expected


@pytest.mark.parametrize(
    "expression",
    [
        '__import__("os")',
        "input.__class__",
        "[item for item in input.items]",
        "input.value + 1",
        "input.value is null",
        "input.route not in input.allowed",
        'input["route"] == "left"',
    ],
)
def test_expression_rejects_syntax_outside_the_closed_language(expression: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate_expression(expression, {"input": {"items": [], "value": 1}})


def test_expression_rejects_more_than_2048_characters() -> None:
    with pytest.raises(ExpressionError, match="2,048"):
        evaluate_expression("true" + " " * 2045, {})


def test_expression_accepts_exactly_2048_characters() -> None:
    assert evaluate_expression("true" + " " * 2044, {}) is True


def test_expression_errors_share_the_package_failure_boundary() -> None:
    with pytest.raises(GraphEngineError) as caught:
        evaluate_expression('__import__("os")', {})
    assert isinstance(caught.value, ValueError)


def test_compile_rejects_unknown_entrypoint(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["entrypoints"]["main"] = "missing"  # type: ignore[index]
    with pytest.raises(CompileError, match="unknown graph missing"):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_rejects_unknown_start_node(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["start"] = "missing"  # type: ignore[index]
    with pytest.raises(CompileError, match="unknown start node root/missing"):
        compile_workflow(_parse_raw(raw), registry)


@pytest.mark.parametrize("endpoint", ["from", "to"])
def test_compile_rejects_unknown_edge_endpoint(registry: CapabilityRegistry, endpoint: str) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["edges"][0][endpoint] = "missing"  # type: ignore[index]
    with pytest.raises(CompileError, match=rf"unknown edge {endpoint} root/missing"):
        compile_workflow(_parse_raw(raw), registry)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("retry", "missing", "unknown retry policy missing"),
        ("timeout", "missing", "unknown timeout policy missing"),
        ("capability", "toy.one.missing", "unknown capability toy.one.missing"),
    ],
)
def test_compile_rejects_unknown_task_reference(
    registry: CapabilityRegistry, field: str, replacement: str, message: str
) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"][field] = replacement  # type: ignore[index]
    with pytest.raises(CompileError, match=message):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_rejects_unknown_subgraph(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"] = {"kind": "subgraph", "graph": "missing"}  # type: ignore[index]
    with pytest.raises(CompileError, match="unknown subgraph missing"):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_rejects_unknown_validator(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"]["validators"] = ["toy.one.missing"]  # type: ignore[index]
    with pytest.raises(CompileError, match="unknown validator toy.one.missing"):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_accepts_registered_validator(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"]["validators"] = ["toy.one.clean"]  # type: ignore[index]
    compile_workflow(_parse_raw(raw), registry)


def test_compile_rejects_outgoing_edge_from_end(registry: CapabilityRegistry) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["edges"].append({"from": "done", "to": "ping"})  # type: ignore[index]
    with pytest.raises(CompileError, match="end node root/done has outgoing edge"):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_requires_two_distinct_sources_for_all_join(
    registry: CapabilityRegistry,
) -> None:
    raw = _raw_valid()
    graph = raw["graphs"]["root"]  # type: ignore[index]
    graph["nodes"]["joined"] = {"kind": "join", "join": "all"}  # type: ignore[index]
    graph["edges"] = [  # type: ignore[index]
        {"from": "ping", "to": "joined"},
        {"from": "ping", "to": "joined"},
        {"from": "joined", "to": "done"},
    ]
    with pytest.raises(CompileError, match="all join root/joined requires two distinct incoming sources"):
        compile_workflow(_parse_raw(raw), registry)


def test_compile_accepts_all_join_with_two_distinct_sources(
    registry: CapabilityRegistry,
) -> None:
    raw = _raw_valid()
    graph = raw["graphs"]["root"]  # type: ignore[index]
    graph["nodes"] = {  # type: ignore[index]
        "gate": {"kind": "gate", "expression": "true"},
        "left": {"kind": "gate", "expression": "true"},
        "right": {"kind": "gate", "expression": "true"},
        "joined": {"kind": "join", "join": "all"},
        "done": {"kind": "end"},
    }
    graph["start"] = "gate"  # type: ignore[index]
    graph["edges"] = [  # type: ignore[index]
        {"from": "gate", "to": "left"},
        {"from": "gate", "to": "right"},
        {"from": "left", "to": "joined"},
        {"from": "right", "to": "joined"},
        {"from": "joined", "to": "done"},
    ]
    compile_workflow(_parse_raw(raw), registry)


def test_reachable_cycle_has_stable_declaration_order_ranks(
    registry: CapabilityRegistry,
) -> None:
    raw = _raw_valid()
    graph = raw["graphs"]["root"]  # type: ignore[index]
    graph["nodes"] = {  # type: ignore[index]
        "first": {"kind": "gate", "expression": "true"},
        "second": {"kind": "gate", "expression": "true"},
        "done": {"kind": "end"},
    }
    graph["start"] = "first"  # type: ignore[index]
    graph["edges"] = [  # type: ignore[index]
        {"from": "first", "to": "second"},
        {"from": "second", "to": "first"},
        {"from": "second", "to": "done"},
    ]

    first = compile_workflow(_parse_raw(raw), registry)
    second = compile_workflow(_parse_raw(deepcopy(raw)), registry)

    assert first.digest == second.digest
    assert first.graphs["root"].nodes["first"].topology_rank == 0
    assert first.graphs["root"].nodes["second"].topology_rank == 1
    assert first.graphs["root"].nodes["done"].topology_rank == 2


def test_deep_reachable_cycle_compiles_without_python_recursion(
    registry: CapabilityRegistry,
) -> None:
    node_count = sys.getrecursionlimit() + 100
    node_ids = [f"node-{index:04d}" for index in range(node_count)]
    raw = _raw_valid()
    graph = raw["graphs"]["root"]  # type: ignore[index]
    graph["max_activations"] = 10_000  # type: ignore[index]
    graph["start"] = node_ids[0]  # type: ignore[index]
    graph["nodes"] = {node_id: {"kind": "gate", "expression": "true"} for node_id in node_ids}  # type: ignore[index]
    graph["edges"] = [  # type: ignore[index]
        {"from": source, "to": target} for source, target in zip(node_ids[:-1], node_ids[1:], strict=True)
    ]
    graph["edges"].append({"from": node_ids[-1], "to": node_ids[-2]})  # type: ignore[index]

    first = compile_workflow(_parse_raw(raw), registry)
    second = compile_workflow(_parse_raw(deepcopy(raw)), registry)

    assert first.digest == second.digest
    assert first.graphs["root"].nodes[node_ids[0]].topology_rank == 0
    assert first.graphs["root"].nodes[node_ids[-2]].topology_rank == node_count - 2
    assert first.graphs["root"].nodes[node_ids[-1]].topology_rank == node_count - 1


def test_compiled_values_are_immutable_and_json_serializable(
    registry: CapabilityRegistry,
) -> None:
    compiled = compile_workflow(parse_workflow(VALID), registry)
    graph = compiled.graphs["root"]
    node = graph.nodes["ping"]

    with pytest.raises(TypeError):
        compiled.graphs["other"] = graph
    with pytest.raises(TypeError):
        graph.nodes["other"] = node
    with pytest.raises(TypeError):
        node.definition.input["new"] = True
    with pytest.raises(ValidationError, match="frozen"):
        node.topology_rank = 99

    dumped = compiled.model_dump(mode="json", by_alias=True)
    assert json.loads(json.dumps(dumped)) == dumped
    digest = dumped.pop("digest")
    from graph_engine.canonical import canonical_digest

    assert canonical_digest(dumped) == digest


def test_compiled_mappings_have_an_honest_read_only_contract(
    registry: CapabilityRegistry,
) -> None:
    compiled = compile_workflow(parse_workflow(VALID), registry)
    graph = compiled.graphs["root"]

    assert isinstance(compiled.graphs, Mapping)
    assert not isinstance(compiled.graphs, dict)
    assert isinstance(graph.nodes, Mapping)
    assert not isinstance(graph.nodes, dict)
    with pytest.raises(TypeError):
        dict.__setitem__(compiled.graphs, "other", graph)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dict.__setitem__(graph.nodes, "other", graph.nodes["ping"])  # type: ignore[arg-type]

    dumped = compiled.model_dump(mode="json", by_alias=True)
    digest = dumped.pop("digest")
    from graph_engine.canonical import canonical_digest

    assert canonical_digest(dumped) == digest


def test_compiled_nested_input_is_immutable_after_all_validation_paths(
    registry: CapabilityRegistry,
) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"]["input"] = {  # type: ignore[index]
        "nested": {"items": ["one", "two"]}
    }
    compiled = compile_workflow(_parse_raw(raw), registry)
    reconstructed = (
        CompiledWorkflow.model_validate(compiled.model_dump(mode="json", by_alias=True)),
        CompiledWorkflow.model_validate_json(compiled.model_dump_json(by_alias=True)),
    )

    for candidate in (compiled, *reconstructed):
        node_input = candidate.graphs["root"].nodes["ping"].definition.input
        nested = cast(Mapping[str, object], node_input["nested"])
        items = cast(tuple[object, ...], nested["items"])
        assert not isinstance(node_input, dict)
        assert not isinstance(nested, dict)
        assert isinstance(items, tuple)
        with pytest.raises(TypeError):
            node_input["new"] = True
        with pytest.raises(TypeError):
            nested["new"] = True  # type: ignore[index]
        with pytest.raises(TypeError):
            items[0] = "changed"  # type: ignore[index]


def test_compiled_node_freezes_a_normally_validated_node_definition() -> None:
    definition = NodeDef.model_validate(
        {
            "kind": "task",
            "capability": "toy.one.ping",
            "retry": "once",
            "timeout": "short",
            "input": {"nested": {"items": ["one", "two"]}},
        }
    )
    node = CompiledNode.model_validate(
        {
            "graph_id": "root",
            "node_id": "ping",
            "declaration_index": 0,
            "topology_rank": 0,
            "definition": definition,
            "incoming": (),
            "outgoing": (),
        }
    )

    nested = cast(Mapping[str, object], node.definition.input["nested"])
    assert not isinstance(node.definition.input, dict)
    assert not isinstance(nested, dict)
    assert isinstance(nested["items"], tuple)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability", None),
        ("graph", None),
        ("join", None),
        ("expression", None),
        ("reason", None),
        ("actions", []),
        ("input", {}),
        ("retry", None),
        ("timeout", None),
        ("resources", {}),
        ("validators", []),
    ],
)
def test_compiled_node_rejects_forbidden_explicit_default_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        CompiledNode.model_validate(
            {
                "graph_id": "root",
                "node_id": "done",
                "declaration_index": 0,
                "topology_rank": 0,
                "definition": {"kind": "end", field: value},
                "incoming": (),
                "outgoing": (),
            }
        )


def test_valid_compiled_json_round_trip_is_sparse_and_immutable(
    registry: CapabilityRegistry,
) -> None:
    raw = _raw_valid()
    raw["graphs"]["root"]["nodes"]["ping"]["input"] = {  # type: ignore[index]
        "nested": {"items": ["one", "two"]}
    }
    compiled = compile_workflow(_parse_raw(raw), registry)

    serialized = compiled.model_dump_json(by_alias=True)
    serialized_object = json.loads(serialized)
    assert serialized_object["graphs"]["root"]["nodes"]["done"]["definition"] == {"kind": "end"}

    reconstructed = CompiledWorkflow.model_validate_json(serialized)
    assert reconstructed.model_dump_json(by_alias=True) == serialized
    node_input = reconstructed.graphs["root"].nodes["ping"].definition.input
    nested = cast(Mapping[str, object], node_input["nested"])
    assert not isinstance(node_input, dict)
    assert not isinstance(nested, dict)
    assert isinstance(nested["items"], tuple)


def test_compiled_edges_and_adjacency_are_tuples(registry: CapabilityRegistry) -> None:
    graph = compile_workflow(parse_workflow(VALID), registry).graphs["root"]
    assert isinstance(graph.edges, tuple)
    assert isinstance(graph.nodes["ping"].incoming, tuple)
    assert isinstance(graph.nodes["ping"].outgoing, tuple)
