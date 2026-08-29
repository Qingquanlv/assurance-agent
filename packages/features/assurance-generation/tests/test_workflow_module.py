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

from assurance_generation.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_generation.plugin import GenerationPlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_GOLDEN = json.loads(
    (_WORKTREE / "tests/product/goldens/assurance-full-pre-modular.json").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation"
_MODULE_ID = "assurance.generation.workflow"
_RESOURCE_ID = "assurance.generation.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = tuple(_OWNERSHIP["owners"]["generation"]["graphs"])
_BASES = tuple(AGENT_JOB_CONTRACTS)
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("generate",)
_FAMILIES = ("api", "e2e", "fuzz", "performance")
_LANE_GRAPHS = tuple(f"generation-{family}" for family in _FAMILIES)
_LEAF_GRAPHS = tuple(graph_id for graph_id in _OWNED_GRAPHS if graph_id not in {"generation", *_LANE_GRAPHS})
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_PUBLIC_INPUT = {
    "change_id": "CH-DEMO-001",
    "selected_test_families": ["api"],
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
    return GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


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


def _job_base(graph_id: str) -> str:
    rest = graph_id.removeprefix("generation-")
    family, _, job = rest.partition("-")
    return f"{family}.{job}"


def test_generation_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.generation"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.1.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["generate"].graph == "generation"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert len(_OWNED_GRAPHS) == 19
    assert _OWNERSHIP["owners"]["generation"]["exports"]["generate"]["target"] == "generation"

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
        f"{base}.{phase}": f"assurance.generation.agent.{base}.v1" for base in _BASES for phase in _PHASES
    }
    assert {name: slot.contract_id for name, slot in module.capability_slots.items()} == expected_slots
    assert len(module.capability_slots) == 42
    assert len(_BASES) == 14
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


def test_generation_feature_forbids_product_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["api.plan"].__class__ is AgentExecutionContract


def test_generate_input_carries_closed_nonempty_family_selection_and_root_dependencies() -> None:
    schema = json.loads(_schema(_io_schema_id("generate", "input")).content)
    pointers = {
        item["pointer"] for item in _OWNERSHIP["exported_closures"]["generation.generate"]["root_pointers"]
    }
    assert pointers == {
        "/allowed_artifact_paths",
        "/capability_leafs",
        "/change_id",
        "/selected_test_families",
    }
    required = set(schema["required"])
    assert required == {"allowed_artifact_paths", "capability_leafs", "change_id", "selected_test_families"}
    families = schema["properties"]["selected_test_families"]
    assert families["type"] == "array"
    assert families["minItems"] == 1
    assert families["items"]["enum"] == list(_FAMILIES)


def test_every_generation_local_subgraph_call_projects_child_required_fields() -> None:
    module = _load_module()
    generation_calls = [item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS]
    assert {(item["graph"], item["node"], item["target"]) for item in generation_calls} == {
        ("generation", "api", "generation-api"),
        ("generation", "e2e", "generation-e2e"),
        ("generation", "fuzz", "generation-fuzz"),
        ("generation", "performance", "generation-performance"),
        ("generation-api", "plan", "generation-api-plan"),
        ("generation-api", "plan-review", "generation-api-plan-review"),
        ("generation-api", "codegen", "generation-api-codegen"),
        ("generation-api", "codegen-fix", "generation-api-codegen-fix"),
        ("generation-e2e", "plan", "generation-e2e-plan"),
        ("generation-e2e", "plan-review", "generation-e2e-plan-review"),
        ("generation-e2e", "codegen", "generation-e2e-codegen"),
        ("generation-e2e", "codegen-fix", "generation-e2e-codegen-fix"),
        ("generation-fuzz", "plan", "generation-fuzz-plan"),
        ("generation-fuzz", "plan-review", "generation-fuzz-plan-review"),
        ("generation-fuzz", "codegen", "generation-fuzz-codegen"),
        ("generation-performance", "plan", "generation-performance-plan"),
        ("generation-performance", "plan-review", "generation-performance-plan-review"),
        ("generation-performance", "codegen", "generation-performance-codegen"),
    }
    for item in generation_calls:
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


def test_selected_lane_root_to_plan_review_codegen_propagates_family_budget_and_artifact_refs() -> None:
    module = _load_module()
    root = module.graphs["generation"]
    lane = module.graphs["generation-api"]
    plan = module.graphs["generation-api-plan"]
    review = module.graphs["generation-api-plan-review"]
    codegen = module.graphs["generation-api-codegen"]

    select_api = _as_mapping(
        project_task_input(
            _require_projection(root.nodes["select-api"].input_projection),
            root_input={"selected_test_families": ["e2e"], "leak_token": "root"},
            graph_input=_PUBLIC_INPUT,
            node_config={},
            predecessor_tokens={},
        )
    )
    assert select_api["selected_test_families"] == ["api"]
    assert "leak_token" not in select_api
    assert "budgets" not in select_api

    api_input = _as_mapping(
        project_task_input(
            _require_projection(root.nodes["api"].input_projection),
            root_input={"change_id": "ROOT-LEAK", "leak_token": "root"},
            graph_input=_PUBLIC_INPUT,
            node_config={},
            predecessor_tokens={},
        )
    )
    assert api_input["change_id"] == "CH-DEMO-001"
    assert api_input["allowed_artifact_paths"] == ["qa/changes"]
    assert api_input["capability_leafs"] == ["entities.item.create"]
    assert "leak_token" not in api_input
    assert "budgets" not in api_input

    hop_inputs: dict[str, Mapping[str, object]] = {}
    for node_id, child in (("plan", plan), ("plan-review", review), ("codegen", codegen)):
        child_input = _as_mapping(
            project_task_input(
                _require_projection(lane.nodes[node_id].input_projection),
                root_input=_PUBLIC_INPUT,
                graph_input=api_input,
                node_config={},
                predecessor_tokens={},
            )
        )
        hop_inputs[node_id] = child_input
        assert child_input["change_id"] == "CH-DEMO-001"
        assert child_input["allowed_artifact_paths"] == ["qa/changes"]
        assert child_input["capability_leafs"] == ["entities.item.create"]
        assert "leak_token" not in child_input
        assert "budgets" not in child_input

        prepare = _as_mapping(
            project_task_input(
                _require_projection(child.nodes["prepare"].input_projection),
                root_input=_PUBLIC_INPUT,
                graph_input=child_input,
                node_config={},
                predecessor_tokens={},
            )
        )
        assert prepare["change_id"] == "CH-DEMO-001"
        assert prepare["artifact_paths"] == ["qa/changes"]
        assert "leak_token" not in prepare
        assert "budgets" not in prepare

    assert hop_inputs["plan"] == hop_inputs["plan-review"] == hop_inputs["codegen"]


def test_generation_keeps_four_fixed_lanes_and_structural_skip_join() -> None:
    module = _load_module()
    root = module.graphs["generation"]
    golden = cast(dict[str, dict[str, object]], _GOLDEN["graphs"]["generation"]["nodes"])
    for family in _FAMILIES:
        lane = root.nodes[family]
        assert lane.kind == "subgraph"
        assert lane.graph == f"generation-{family}"
        assert root.nodes[f"select-{family}"].expression == golden[f"select-{family}"]["expression"]
        assert root.nodes[f"{family}-skip"].kind == "gate"
        assert root.nodes[f"{family}-skip"].expression == "true"
        assert root.nodes[f"{family}-done"].kind == "gate"
        assert root.nodes[f"{family}-done"].expression == "true"
    join = root.nodes["join-selected"]
    assert join.kind == "join"
    assert join.join == "all"
    assert sum(1 for node in root.nodes.values() if node.kind == "join") == 1
    actual_edges = {_edge_record(edge) for edge in root.edges}
    for family in _FAMILIES:
        assert (f"select-{family}", family, "output.value == true") in actual_edges
        assert (f"select-{family}", f"{family}-skip", "output.value == false") in actual_edges
        assert (family, f"{family}-done", None) in actual_edges
        assert (f"{family}-skip", f"{family}-done", None) in actual_edges
        assert (f"{family}-done", "join-selected", None) in actual_edges
    assert ("join-selected", "done", None) in actual_edges


def test_review_and_codegen_outcomes_stay_on_the_legacy_expressions() -> None:
    module = _load_module()
    for family, has_fix in (("api", True), ("e2e", True), ("fuzz", False), ("performance", False)):
        lane = module.graphs[f"generation-{family}"]
        golden = cast(dict[str, dict[str, object]], _GOLDEN["graphs"][f"generation-{family}"]["nodes"])
        for node_id in ("plan-review-pass-gate", "plan-review-fix-gate", "plan-review-human-gate"):
            assert lane.nodes[node_id].expression == golden[node_id]["expression"]
        human = lane.nodes["plan-human-review"]
        assert human.kind == "interrupt"
        assert human.reason == golden["plan-human-review"]["reason"]
        assert tuple(human.actions) == tuple(cast(list[object], golden["plan-human-review"]["actions"]))
        if has_fix:
            assert lane.nodes["codegen-pass-gate"].expression == golden["codegen-pass-gate"]["expression"]
            assert lane.nodes["codegen-fix-gate"].expression == golden["codegen-fix-gate"]["expression"]
            assert "codegen-fix" in lane.nodes
        else:
            assert "codegen-fix" not in lane.nodes
            assert "codegen-pass-gate" not in lane.nodes


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    assert set(_LEAF_GRAPHS) == set(f"generation-{base.replace('.', '-')}" for base in _BASES)
    for graph_id in _LEAF_GRAPHS:
        nodes = module.graphs[graph_id].nodes
        base = _job_base(graph_id)
        assert nodes["prepare"].capability == f"assurance.generation.{base}.prepare"
        assert nodes["prepare"].capability_slot is None
        assert nodes["finalize"].capability == f"assurance.generation.{base}.finalize"
        assert nodes["finalize"].capability_slot is None
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{base}.execute"


def test_export_output_projection_exposes_normalized_per_family_aggregate() -> None:
    module = _load_module()
    projection = module.exports["generate"].output_projection
    assert isinstance(projection, OutputObjectProjection)
    assert set(projection.fields) == {"families", "selected_families"}
    for field in projection.fields.values():
        assert isinstance(field, ChildOutputPointerProjection)
    schema = json.loads(_schema(_io_schema_id("generate", "output")).content)
    assert set(schema["properties"]) == {"families", "selected_families"}
    assert schema["properties"]["selected_families"]["items"]["enum"] == list(_FAMILIES)
    assert set(schema["properties"]["families"]["properties"]) == set(_FAMILIES)


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
                capability_id = f"test.generation.slot.{slot}"
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
        name="generation-isolation",
        entrypoints={"generate": "generation"},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    compiled = compile_workflow(workflow, _Capabilities())
    assert set(compiled.graphs) == set(_OWNED_GRAPHS)
    payload = compiled.model_dump(mode="json", by_alias=True)
    assert "capability_slot" not in json.dumps(payload)
    assert "assurance.product.agent." not in json.dumps(payload)
