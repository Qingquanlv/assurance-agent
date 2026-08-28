from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import yaml

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.graph import compile_workflow, parse_workflow_module, project_task_input
from graph_engine.graph.compiler import CompiledWorkflow
from graph_engine.graph.input_projection import (
    GraphInputPointerProjection,
    InputProjectionDef,
    ObjectProjection,
    RootPointerProjection,
)
from graph_engine.graph.module_schema import WorkflowModuleDef
from graph_engine.graph.output_projection import (
    ChildOutputPointerProjection,
    ObjectProjection as OutputObjectProjection,
)
from graph_engine.graph.schema import EdgeDef, NodeDef, WorkflowDef
from graph_engine.plugin_api import (
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_execution.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_execution.plugin import ExecutionPlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_execution"
_MODULE_ID = "assurance.execution.workflow"
_RESOURCE_ID = "assurance.execution.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = tuple(_OWNERSHIP["owners"]["execution"]["graphs"])
_BASES = tuple(AGENT_JOB_CONTRACTS)
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("execute", "rerun")
_GRAPH_BY_EXPORT = {"execute": "execution-execute", "rerun": "execution-run"}
_BASE_BY_GRAPH = {"execution-execute": "execute", "execution-run": "run"}
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_PRODUCT_STATUSES = ("product_issue", "infrastructure_failure", "succeeded")
_FAMILIES = ("api", "e2e", "fuzz", "performance")
_EXECUTE_INPUT = {
    "change_id": "CH-EXECUTE-001",
    "selected_test_families": ["api"],
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "budgets": {"coverage_rounds": 2, "review_rounds": 1},
    "leak_token": "must-not-cross-execute-boundary",
}
_RERUN_INPUT = {
    "change_id": "CH-RERUN-002",
    "selected_test_families": ["e2e", "fuzz"],
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": ["qa/archive"],
    "budgets": {"coverage_rounds": 9, "review_rounds": 4},
    "leak_token": "must-not-cross-rerun-boundary",
}


class _FakeSlotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"status": "ok"})


def _contribution():
    return ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def _module_resource() -> ResourceContribution:
    for resource in _contribution().resources:
        if resource.resource_id == _RESOURCE_ID:
            return resource
    raise AssertionError(f"missing workflow module resource {_RESOURCE_ID}")


def _schema(schema_id: str) -> SchemaContribution:
    for schema in _contribution().schemas:
        if schema.schema_id == schema_id:
            return schema
    raise AssertionError(f"missing workflow I/O schema {schema_id}")


def _load_module() -> WorkflowModuleDef:
    resource = _module_resource()
    assert resource.media_type == _MODULE_MIME
    return parse_workflow_module(resource.content)


def _io_schema_id(export: str, direction: str) -> str:
    return f"{_MODULE_ID}.{export}.{direction}.v1"


def _walk_projection(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, ObjectProjection):
        for field in value.fields.values():
            yield from _walk_projection(field)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_projection(item)


def _graph_input_fields(node: NodeDef) -> set[str]:
    fields: set[str] = set()
    for item in _walk_projection(node.input_projection):
        if isinstance(item, GraphInputPointerProjection) and item.pointer.startswith("/"):
            fields.add(item.pointer[1:].split("/", 1)[0])
        if isinstance(item, RootPointerProjection):
            raise AssertionError("Feature graphs still contain root_pointer")
    return fields


def _imported_modules(path: Path) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def _edge_record(edge: Mapping[str, object] | EdgeDef) -> tuple[str, str, str | None]:
    if isinstance(edge, Mapping):
        condition = edge.get("condition")
        return (str(edge["from"]), str(edge["to"]), str(condition) if condition is not None else None)
    return (edge.from_, edge.to, edge.condition)


def _require_projection(projection: InputProjectionDef | None) -> InputProjectionDef:
    assert projection is not None
    return projection


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _bind_and_compile(module: WorkflowModuleDef, graph_id: str, export: str) -> CompiledWorkflow:
    handlers: dict[str, object] = {}
    graph = module.graphs[graph_id]
    nodes = {}
    for node_id, node in graph.nodes.items():
        payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
        slot = payload.pop("capability_slot", None)
        if slot is not None:
            capability_id = f"test.execution.slot.{slot}"
            payload["capability"] = capability_id
            handlers[capability_id] = _FakeSlotHandler()
        elif node.capability is not None:
            handlers[node.capability] = _FakeSlotHandler()
        nodes[node_id] = NodeDef.model_validate(payload)
    graphs = {graph_id: graph.model_copy(update={"nodes": nodes})}

    class _Capabilities:
        task_handlers = handlers
        commit_validators: dict[str, object] = {}

    workflow = WorkflowDef(
        name=f"execution-{export}-isolation",
        entrypoints={export: graph_id},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    return compile_workflow(workflow, _Capabilities())


def test_execution_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.execution"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.1.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["execute"].graph == "execution-execute"
    assert module.exports["rerun"].graph == "execution-run"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert _OWNED_GRAPHS == ("execution-execute", "execution-run")
    assert _OWNERSHIP["owners"]["execution"]["exports"]["execute"]["target"] == "execution-execute"
    assert _OWNERSHIP["owners"]["execution"]["exports"]["rerun"]["target"] == "execution-run"

    for export in _EXPORTS:
        declared = module.exports[export]
        assert declared.input_schema == _io_schema_id(export, "input")
        assert declared.output_schema == _io_schema_id(export, "output")
        schema_in = json.loads(_schema(declared.input_schema).content)
        schema_out = json.loads(_schema(declared.output_schema).content)
        assert schema_in["additionalProperties"] is False
        assert schema_out["additionalProperties"] is False
        assert declared.input_schema in module.schemas
        assert declared.output_schema in module.schemas

    expected_slots = {
        f"{base}.{phase}": f"assurance.execution.agent.{base}.v1" for base in _BASES for phase in _PHASES
    }
    assert {name: slot.contract_id for name, slot in module.capability_slots.items()} == expected_slots
    assert len(module.capability_slots) == 6
    assert len(_BASES) == 2
    for base, contract in AGENT_JOB_CONTRACTS.items():
        for phase in _PHASES:
            assert module.capability_slots[f"{base}.{phase}"].contract_id == contract.contract_id

    dumped = json.dumps(module.model_dump(mode="json", by_alias=True), ensure_ascii=True)
    assert "root_pointer" not in dumped
    assert "assurance.product.agent." not in dumped
    raw = resource.content.decode("utf-8")
    assert "root_pointer" not in raw
    assert "assurance.product.agent." not in raw
    assert "name:" not in raw.split("graphs:", 1)[0]


def test_execution_feature_forbids_product_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["execute"].__class__ is AgentExecutionContract


def test_execute_and_rerun_inputs_carry_closed_policy_and_inventory_refs() -> None:
    for export, closure in (("execute", "execution.execute"), ("rerun", "execution.rerun")):
        pointers = {item["pointer"] for item in _OWNERSHIP["exported_closures"][closure]["root_pointers"]}
        assert pointers == {"/capability_leafs", "/change_id", "/selected_test_families"}
        schema = json.loads(_schema(_io_schema_id(export, "input")).content)
        required = set(schema["required"])
        assert required == {"capability_leafs", "change_id", "selected_test_families"}
        families = schema["properties"]["selected_test_families"]
        assert families["type"] == "array"
        assert families["minItems"] == 1
        assert families["items"]["enum"] == list(_FAMILIES)


def test_execution_exports_are_independent_task_triplets() -> None:
    module = _load_module()
    execution_calls = [item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS]
    assert execution_calls == []
    assert {item["target"] for item in _OWNERSHIP["local_subgraph_calls"]}.isdisjoint(_OWNED_GRAPHS)

    for graph_id, other in (
        ("execution-execute", "execution-run"),
        ("execution-run", "execution-execute"),
    ):
        graph = module.graphs[graph_id]
        assert set(graph.nodes) == {"prepare", "execute", "finalize", "done"}
        assert graph.start == "prepare"
        assert graph.nodes["done"].kind == "end"
        assert all(node.kind != "subgraph" for node in graph.nodes.values())
        assert all(getattr(node, "graph", None) != other for node in graph.nodes.values())
        assert {_edge_record(edge) for edge in graph.edges} == {
            ("prepare", "execute", None),
            ("execute", "finalize", None),
            ("finalize", "done", None),
        }


def test_each_export_reads_policy_and_refs_from_its_own_graph_input() -> None:
    module = _load_module()
    samples = {
        "execute": (_EXECUTE_INPUT, _RERUN_INPUT),
        "rerun": (_RERUN_INPUT, _EXECUTE_INPUT),
    }
    projected: dict[str, Mapping[str, object]] = {}
    for export, (graph_input, foreign) in samples.items():
        graph = module.graphs[_GRAPH_BY_EXPORT[export]]
        for node_id in ("prepare", "finalize"):
            payload = _as_mapping(
                project_task_input(
                    _require_projection(graph.nodes[node_id].input_projection),
                    root_input=foreign,
                    graph_input=graph_input,
                    node_config={},
                    predecessor_tokens={"execute": {"status": "passed"}} if node_id == "finalize" else {},
                )
            )
            assert payload["change_id"] == graph_input["change_id"]
            assert payload["selected_test_families"] == graph_input["selected_test_families"]
            assert payload["capability_leafs"] == graph_input["capability_leafs"]
            assert "leak_token" not in payload
            assert "budgets" not in payload
            assert "allowed_artifact_paths" not in payload
            if node_id == "prepare":
                projected[export] = payload
                assert set(_graph_input_fields(graph.nodes[node_id])) == {
                    "capability_leafs",
                    "change_id",
                    "selected_test_families",
                }
    assert projected["execute"]["change_id"] != projected["rerun"]["change_id"]
    assert projected["execute"]["selected_test_families"] != projected["rerun"]["selected_test_families"]
    assert projected["execute"]["capability_leafs"] != projected["rerun"]["capability_leafs"]


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    for graph_id, base in _BASE_BY_GRAPH.items():
        nodes = module.graphs[graph_id].nodes
        assert nodes["prepare"].capability == f"assurance.execution.{base}.prepare"
        assert nodes["prepare"].capability_slot is None
        assert nodes["finalize"].capability == f"assurance.execution.{base}.finalize"
        assert nodes["finalize"].capability_slot is None
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{base}.execute"


def test_public_verdict_schema_is_exactly_passed_or_failed() -> None:
    module = _load_module()
    for export in _EXPORTS:
        projection = module.exports[export].output_projection
        assert isinstance(projection, OutputObjectProjection)
        assert set(projection.fields) == {"status"}
        assert isinstance(projection.fields["status"], ChildOutputPointerProjection)
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        dumped = json.dumps(schema)
        for invented in _PRODUCT_STATUSES:
            assert invented not in dumped
        assert schema["properties"]["status"]["enum"] == ["failed", "passed"]
        assert set(schema["properties"]) == {"status"}


def test_fake_slot_bindings_compile_each_export_without_an_agent_server() -> None:
    module = _load_module()
    for export, graph_id in _GRAPH_BY_EXPORT.items():
        compiled = _bind_and_compile(module, graph_id, export)
        assert set(compiled.graphs) == {graph_id}
        payload = json.dumps(compiled.model_dump(mode="json", by_alias=True))
        assert "capability_slot" not in payload
        assert "assurance.product.agent." not in payload
        assert ("execution-run" if graph_id == "execution-execute" else "execution-execute") not in payload
