from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import yaml

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.graph import compile_workflow, parse_workflow_module, project_task_input
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
from graph_engine.graph.schema import EdgeDef, NodeDef
from graph_engine.plugin_api import (
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_intake.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_intake.plugin import IntakePlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_GOLDEN = json.loads(
    (_WORKTREE / "tests/product/goldens/assurance-full-pre-modular.json").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_intake"
_MODULE_ID = "assurance.intake.workflow"
_RESOURCE_ID = "assurance.intake.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = ("entry", "case", "intake", "explore", "case-design", "case-review")
_BASES = ("case-design", "case-review", "explore", "intake")
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("prepare", "case")
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_PUBLIC_INPUT = {
    "change_id": "CH-DEMO-001",
    "requirement": "Cover department CRUD.",
    "selected_test_families": ["api"],
    "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "budgets": {"coverage_rounds": 2, "review_rounds": 1},
    "leak_token": "must-not-cross-subgraph-boundary",
}


class _FakeSlotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"status": "ok"})


def _contribution():
    return IntakePlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


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


def test_intake_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.intake"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.1.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["prepare"].graph == "entry"
    assert module.exports["case"].graph == "case"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert tuple(_OWNERSHIP["owners"]["intake"]["graphs"]) == _OWNED_GRAPHS
    assert _OWNERSHIP["owners"]["intake"]["exports"]["prepare"]["target"] == "entry"
    assert _OWNERSHIP["owners"]["intake"]["exports"]["case"]["target"] == "case"

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
        f"{base}.{phase}": f"assurance.intake.agent.{base}.v1" for base in _BASES for phase in _PHASES
    }
    assert {name: slot.contract_id for name, slot in module.capability_slots.items()} == expected_slots
    assert len(module.capability_slots) == 12
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


def test_intake_feature_forbids_product_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["intake"].__class__ is AgentExecutionContract


def test_entry_owns_the_relocated_full_intake_prefix() -> None:
    module = _load_module()
    entry = module.graphs["entry"]
    relocation = _OWNERSHIP["product_to_intake_relocation"]
    full_nodes = cast(dict[str, dict[str, object]], _GOLDEN["graphs"]["full"]["nodes"])
    prefix = tuple(relocation["prefix_nodes"])
    assert prefix == (
        "intake",
        "explore",
        "case-design",
        "case-review",
        "review-pass-gate",
        "review-fix-gate",
        "review-human-gate",
        "human-review",
    )
    for node_id in prefix:
        assert node_id in entry.nodes
    assert "generation" not in entry.nodes
    assert "done" in entry.nodes
    assert entry.nodes["done"].kind == "end"

    for node_id in ("review-pass-gate", "review-fix-gate", "review-human-gate"):
        assert entry.nodes[node_id].expression == full_nodes[node_id]["expression"]
    human = entry.nodes["human-review"]
    assert human.kind == "interrupt"
    assert human.reason == full_nodes["human-review"]["reason"]
    assert tuple(human.actions) == tuple(cast(list[object], full_nodes["human-review"]["actions"]))

    actual_edges = {_edge_record(edge) for edge in entry.edges}
    expected_internal = {
        _edge_record(cast(Mapping[str, object], item))
        for item in cast(list[object], relocation["prefix_internal_edges"])
    }
    expected_terminal = {
        (item["from"], "done", item.get("condition"))
        for item in cast(list[dict[str, object]], relocation["continuation_edges"])
    }
    assert expected_internal <= actual_edges
    assert expected_terminal <= actual_edges
    assert ("case-review", "done", None) not in actual_edges
    assert all(edge.to != "generation" for edge in entry.edges)

    review = entry.nodes["case-review"]
    assert review.routing is not None
    assert review.routing.mode == "fanout"
    assert review.routing.min_matches == 3


def test_every_intake_local_subgraph_call_projects_child_required_fields() -> None:
    module = _load_module()
    intake_calls = [item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS]
    assert {(item["graph"], item["node"], item["target"]) for item in intake_calls} == {
        ("entry", "intake", "intake"),
        ("entry", "explore", "explore"),
        ("entry", "case-design", "case-design"),
        ("entry", "case-review", "case-review"),
        ("case", "case-design", "case-design"),
        ("case", "case-review", "case-review"),
    }
    for item in intake_calls:
        caller = module.graphs[item["graph"]].nodes[item["node"]]
        assert caller.kind == "subgraph"
        assert caller.graph == item["target"]
        assert isinstance(caller.input_projection, ObjectProjection)
        required = set()
        for _node_id, node in module.graphs[item["target"]].nodes.items():
            required.update(_graph_input_fields(node))
        assert set(caller.input_projection.fields) == required
        for field, projection in caller.input_projection.fields.items():
            assert isinstance(projection, GraphInputPointerProjection)
            assert projection.pointer == f"/{field}"


def test_prepare_to_intake_to_explore_keeps_identity_and_drops_undeclared_fields() -> None:
    module = _load_module()
    entry = module.graphs["entry"]
    intake = module.graphs["intake"]
    explore = module.graphs["explore"]

    intake_input = _as_mapping(
        project_task_input(
            _require_projection(entry.nodes["intake"].input_projection),
            root_input={"change_id": "ROOT-LEAK", "leak_token": "root"},
            graph_input=_PUBLIC_INPUT,
            node_config={},
            predecessor_tokens={},
        )
    )
    explore_input = _as_mapping(
        project_task_input(
            _require_projection(entry.nodes["explore"].input_projection),
            root_input={"change_id": "ROOT-LEAK", "leak_token": "root"},
            graph_input=_PUBLIC_INPUT,
            node_config={},
            predecessor_tokens={},
        )
    )
    assert intake_input["change_id"] == explore_input["change_id"] == "CH-DEMO-001"
    assert intake_input["allowed_artifact_paths"] == explore_input["allowed_artifact_paths"] == ["qa/changes"]
    assert "budgets" not in intake_input
    assert "budgets" not in explore_input
    assert "leak_token" not in intake_input
    assert "leak_token" not in explore_input
    assert "requirement" in intake_input
    assert "requirement" not in explore_input

    intake_prepare = _as_mapping(
        project_task_input(
            _require_projection(intake.nodes["prepare"].input_projection),
            root_input=_PUBLIC_INPUT,
            graph_input=intake_input,
            node_config={},
            predecessor_tokens={},
        )
    )
    explore_prepare = _as_mapping(
        project_task_input(
            _require_projection(explore.nodes["prepare"].input_projection),
            root_input=_PUBLIC_INPUT,
            graph_input=explore_input,
            node_config={},
            predecessor_tokens={},
        )
    )
    assert intake_prepare["change_id"] == explore_prepare["change_id"] == "CH-DEMO-001"
    assert intake_prepare["artifact_paths"] == explore_prepare["artifact_paths"] == ["qa/changes"]
    assert "budgets" not in intake_prepare
    assert "leak_token" not in intake_prepare
    assert "requirement" in intake_prepare
    assert "requirement" not in explore_prepare


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    for graph_id in ("intake", "explore", "case-design", "case-review"):
        nodes = module.graphs[graph_id].nodes
        assert nodes["prepare"].capability == f"assurance.intake.{graph_id}.prepare"
        assert nodes["prepare"].capability_slot is None
        assert nodes["finalize"].capability == f"assurance.intake.{graph_id}.finalize"
        assert nodes["finalize"].capability_slot is None
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{graph_id}.execute"


def test_export_output_projections_expose_review_outcome_and_artifact_refs() -> None:
    module = _load_module()
    for export_name in _EXPORTS:
        projection = module.exports[export_name].output_projection
        assert isinstance(projection, OutputObjectProjection)
        assert set(projection.fields) == {"decision", "artifacts"}
        for field in projection.fields.values():
            assert isinstance(field, ChildOutputPointerProjection)
        schema = json.loads(_schema(_io_schema_id(export_name, "output")).content)
        assert set(schema["properties"]) == {"decision", "artifacts"}


def test_fake_slot_bindings_compile_without_an_agent_server() -> None:
    module = _load_module()
    handlers: dict[str, object] = {}
    graphs = {}
    for graph_id, graph in module.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            slot = payload.pop("capability_slot", None)
            if slot is not None:
                capability_id = f"test.intake.slot.{slot}"
                payload["capability"] = capability_id
                handlers[capability_id] = _FakeSlotHandler()
            elif node.capability is not None:
                handlers[node.capability] = _FakeSlotHandler()
            nodes[node_id] = NodeDef.model_validate(payload)
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})

    class _Capabilities:
        task_handlers = handlers
        commit_validators: dict[str, object] = {}

    from graph_engine.graph.schema import WorkflowDef

    workflow = WorkflowDef(
        name="intake-isolation",
        entrypoints={"prepare": "entry", "case": "case"},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    compiled = compile_workflow(workflow, _Capabilities())
    assert set(compiled.graphs) == set(_OWNED_GRAPHS)
    payload = compiled.model_dump(mode="json", by_alias=True)
    assert "capability_slot" not in json.dumps(payload)
    assert "assurance.product.agent." not in json.dumps(payload)
