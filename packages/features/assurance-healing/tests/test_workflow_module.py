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

from assurance_healing.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_healing.plugin import HealingPlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_healing"
_MODULE_ID = "assurance.healing.workflow"
_RESOURCE_ID = "assurance.healing.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = tuple(_OWNERSHIP["owners"]["healing"]["graphs"])
_BASES = tuple(AGENT_JOB_CONTRACTS)
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("repair-failure", "repair-coverage")
_GRAPH_BY_EXPORT = {
    "repair-failure": "healing-fix-proposal",
    "repair-coverage": "healing-coverage-repair",
}
_BASE_BY_GRAPH = {
    "healing-fix-proposal": "fix-proposal",
    "healing-coverage-repair": "coverage-repair",
}
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "assurance_quality",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_QUALITY_GRAPHS = tuple(_OWNERSHIP["owners"]["quality"]["graphs"])
_REPAIR_STATUSES = ("exhausted", "failed", "needs_review", "not_eligible", "repaired")
_FAILURE_CLASSIFICATIONS = (
    "environment_failure",
    "failed",
    "infrastructure_failure",
    "pending",
    "product_bug",
    "test",
    "test-data",
    "unknown",
)
_COVERAGE_CLASSIFICATIONS = (
    "exhausted",
    "inconclusive",
    "needs_human",
    "repair_required",
    "satisfied",
)
_INVENTORY_POINTERS = {"/allowed_artifact_paths", "/capability_leafs", "/change_id"}
_FAILURE_INPUT = {
    "change_id": "CH-FIX-001",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "classification": "test",
    "fix_eligible": True,
    "budgets": {"coverage_rounds": 2, "failure_rounds": 1},
    "leak_token": "must-not-cross-failure-boundary",
}
_COVERAGE_INPUT = {
    "change_id": "CH-COV-002",
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": ["qa/archive"],
    "classification": "repair_required",
    "fix_eligible": True,
    "budgets": {"coverage_rounds": 9, "failure_rounds": 4},
    "leak_token": "must-not-cross-coverage-boundary",
}


class _FakeSlotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"status": "ok"})


def _contribution():
    return HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


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
            capability_id = f"test.healing.slot.{slot}"
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
        name=f"healing-{export}-isolation",
        entrypoints={export: graph_id},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    return compile_workflow(workflow, _Capabilities())


def test_healing_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.healing"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.1.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["repair-failure"].graph == "healing-fix-proposal"
    assert module.exports["repair-coverage"].graph == "healing-coverage-repair"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert _OWNED_GRAPHS == ("healing-fix-proposal", "healing-coverage-repair")
    assert _OWNERSHIP["owners"]["healing"]["exports"]["repair-failure"]["target"] == "healing-fix-proposal"
    assert _OWNERSHIP["owners"]["healing"]["exports"]["repair-coverage"]["target"] == (
        "healing-coverage-repair"
    )

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
        f"{base}.{phase}": f"assurance.healing.agent.{base}.v1" for base in _BASES for phase in _PHASES
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


def test_healing_feature_forbids_product_quality_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["fix-proposal"].__class__ is AgentExecutionContract


def test_repair_inputs_receive_quality_classification_and_inventory_refs() -> None:
    expected_required = {
        "allowed_artifact_paths",
        "budgets",
        "capability_leafs",
        "change_id",
        "classification",
        "fix_eligible",
    }
    enums = {
        "repair-failure": list(_FAILURE_CLASSIFICATIONS),
        "repair-coverage": list(_COVERAGE_CLASSIFICATIONS),
    }
    for export, closure in (
        ("repair-failure", "healing.repair-failure"),
        ("repair-coverage", "healing.repair-coverage"),
    ):
        pointers = {item["pointer"] for item in _OWNERSHIP["exported_closures"][closure]["root_pointers"]}
        assert pointers == _INVENTORY_POINTERS
        schema = json.loads(_schema(_io_schema_id(export, "input")).content)
        required = set(schema["required"])
        assert required == expected_required
        assert schema["properties"]["classification"]["enum"] == enums[export]
        assert schema["properties"]["fix_eligible"]["type"] == "boolean"
        budgets = schema["properties"]["budgets"]
        assert budgets["additionalProperties"] is False
        assert set(budgets["required"]) == {"coverage_rounds", "failure_rounds"}


def test_healing_exports_are_independent_and_do_not_import_quality() -> None:
    module = _load_module()
    healing_calls = [item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS]
    assert healing_calls == []
    assert {item["target"] for item in _OWNERSHIP["local_subgraph_calls"]}.isdisjoint(_OWNED_GRAPHS)
    assert set(module.graphs).isdisjoint(_QUALITY_GRAPHS)
    assert module.imports == {}

    dumped = json.dumps(module.model_dump(mode="json", by_alias=True), ensure_ascii=True)
    for graph_id in _QUALITY_GRAPHS:
        assert graph_id not in dumped
    assert "assurance.quality" not in dumped

    for graph_id, other in (
        ("healing-fix-proposal", "healing-coverage-repair"),
        ("healing-coverage-repair", "healing-fix-proposal"),
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


def test_each_export_projects_classification_budget_and_change_refs_from_graph_input() -> None:
    module = _load_module()
    samples = {
        "repair-failure": (_FAILURE_INPUT, _COVERAGE_INPUT),
        "repair-coverage": (_COVERAGE_INPUT, _FAILURE_INPUT),
    }
    expected_fields = {
        "allowed_artifact_paths",
        "budgets",
        "capability_leafs",
        "change_id",
        "classification",
        "fix_eligible",
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
                    predecessor_tokens={"execute": {"status": "repaired"}} if node_id == "finalize" else {},
                )
            )
            assert payload["change_id"] == graph_input["change_id"]
            assert payload["capability_leafs"] == graph_input["capability_leafs"]
            assert payload["artifact_paths"] == graph_input["allowed_artifact_paths"]
            assert payload["classification"] == graph_input["classification"]
            assert payload["fix_eligible"] == graph_input["fix_eligible"]
            assert payload["budgets"] == graph_input["budgets"]
            assert "leak_token" not in payload
            assert set(_graph_input_fields(graph.nodes[node_id])) == expected_fields
            if node_id == "prepare":
                projected[export] = payload
    assert projected["repair-failure"]["change_id"] != projected["repair-coverage"]["change_id"]
    assert projected["repair-failure"]["classification"] != projected["repair-coverage"]["classification"]
    assert projected["repair-failure"]["budgets"] != projected["repair-coverage"]["budgets"]
    assert projected["repair-failure"]["capability_leafs"] != projected["repair-coverage"]["capability_leafs"]


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    for graph_id, base in _BASE_BY_GRAPH.items():
        nodes = module.graphs[graph_id].nodes
        assert nodes["prepare"].capability == f"assurance.healing.{base}.prepare"
        assert nodes["prepare"].capability_slot is None
        assert nodes["finalize"].capability == f"assurance.healing.{base}.finalize"
        assert nodes["finalize"].capability_slot is None
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{base}.execute"


def test_public_repair_outcome_vocabulary_is_closed() -> None:
    module = _load_module()
    for export in _EXPORTS:
        projection = module.exports[export].output_projection
        assert isinstance(projection, OutputObjectProjection)
        assert set(projection.fields) == {"change_id", "effect_refs", "status"}
        for field in projection.fields.values():
            assert isinstance(field, ChildOutputPointerProjection)
        assert isinstance(projection.fields["status"], ChildOutputPointerProjection)
        assert projection.fields["status"].pointer == "/status"
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        assert schema["properties"]["status"]["enum"] == list(_REPAIR_STATUSES)
        assert set(schema["properties"]) == {"change_id", "effect_refs", "status"}
        assert schema["required"] == ["change_id", "effect_refs", "status"]


def test_fake_slot_bindings_compile_each_export_without_an_agent_server() -> None:
    module = _load_module()
    for export, graph_id in _GRAPH_BY_EXPORT.items():
        compiled = _bind_and_compile(module, graph_id, export)
        assert set(compiled.graphs) == {graph_id}
        payload = json.dumps(compiled.model_dump(mode="json", by_alias=True))
        assert "capability_slot" not in payload
        assert "assurance.product.agent." not in payload
        other = "healing-coverage-repair" if graph_id == "healing-fix-proposal" else "healing-fix-proposal"
        assert other not in payload
        for quality_graph in _QUALITY_GRAPHS:
            assert quality_graph not in payload
