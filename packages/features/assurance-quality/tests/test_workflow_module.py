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

from assurance_quality.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_quality.plugin import QualityPlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_quality"
_MODULE_ID = "assurance.quality.workflow"
_RESOURCE_ID = "assurance.quality.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = tuple(_OWNERSHIP["owners"]["quality"]["graphs"])
_BASES = tuple(AGENT_JOB_CONTRACTS)
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("assess", "issue-review", "issue-analyze", "issue-reconcile", "report")
_GRAPH_BY_EXPORT = {
    "assess": "quality",
    "issue-review": "issue-review",
    "issue-analyze": "issue-analyze",
    "issue-reconcile": "issue-reconcile",
    "report": "quality-report",
}
_LEAF_GRAPHS = {
    "quality-fact-baseline": "fact-baseline",
    "quality-inspect": "inspect",
    "quality-issue-triage": "issue-triage",
    "quality-issue-analysis": "issue-analysis",
    "quality-report": "report",
}
_EXPORT_CLOSURES = {
    "assess": ("quality", "quality-fact-baseline", "quality-inspect"),
    "issue-review": ("issue-review", "quality-issue-triage"),
    "issue-analyze": ("issue-analyze", "quality-issue-analysis"),
    "issue-reconcile": ("issue-reconcile", "quality-issue-analysis"),
    "report": ("quality-report",),
}
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_HEALING_GRAPHS = tuple(_OWNERSHIP["owners"]["healing"]["graphs"])
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
_COVERAGE_STATES = (
    "exhausted",
    "inconclusive",
    "needs_human",
    "repair_required",
    "satisfied",
)
_INVENTORY_POINTERS = {"/allowed_artifact_paths", "/capability_leafs", "/change_id"}
_INPUT_FIELDS = {
    "allowed_artifact_paths",
    "budgets",
    "capability_leafs",
    "change_id",
    "evidence_refs",
    "execution_status",
    "rounds_budget",
    "rounds_used",
}
_ASSESS_INPUT = {
    "change_id": "CH-ASSESS-001",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "execution_status": "passed",
    "evidence_refs": [{"path": "qa/changes/CH-ASSESS-001/execution/result.json", "digest": "a" * 64}],
    "budgets": {"coverage_rounds": 2, "failure_rounds": 1},
    "rounds_budget": 2,
    "rounds_used": 0,
    "leak_token": "must-not-cross-assess-boundary",
}
_ISSUE_INPUT = {
    "change_id": "CH-ISSUE-002",
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": ["qa/archive"],
    "execution_status": "failed",
    "evidence_refs": [{"path": "qa/archive/CH-ISSUE-002/execution/result.json", "digest": "b" * 64}],
    "budgets": {"coverage_rounds": 9, "failure_rounds": 4},
    "rounds_budget": 4,
    "rounds_used": 1,
    "leak_token": "must-not-cross-issue-boundary",
}
_REPORT_INPUT = {
    "change_id": "CH-REPORT-003",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "execution_status": "passed",
    "evidence_refs": [{"path": "qa/changes/CH-REPORT-003/inspect/inspection.json", "digest": "c" * 64}],
    "budgets": {"coverage_rounds": 3, "failure_rounds": 2},
    "rounds_budget": 3,
    "rounds_used": 0,
    "leak_token": "must-not-cross-report-boundary",
}


class _FakeSlotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"status": "ok"})


def _contribution():
    return QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


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


def _bind_and_compile(module: WorkflowModuleDef, export: str) -> CompiledWorkflow:
    handlers: dict[str, object] = {}
    graphs = {}
    for graph_id in _EXPORT_CLOSURES[export]:
        graph = module.graphs[graph_id]
        nodes = {}
        for node_id, node in graph.nodes.items():
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            slot = payload.pop("capability_slot", None)
            if slot is not None:
                capability_id = f"test.quality.slot.{slot}"
                payload["capability"] = capability_id
                handlers[capability_id] = _FakeSlotHandler()
            elif node.capability is not None:
                handlers[node.capability] = _FakeSlotHandler()
            nodes[node_id] = NodeDef.model_validate(payload)
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})

    class _Capabilities:
        task_handlers = handlers
        commit_validators: dict[str, object] = {}

    workflow = WorkflowDef(
        name=f"quality-{export}-isolation",
        entrypoints={export: _GRAPH_BY_EXPORT[export]},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    return compile_workflow(workflow, _Capabilities())


def test_quality_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.quality"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.1.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["assess"].graph == "quality"
    assert module.exports["issue-review"].graph == "issue-review"
    assert module.exports["issue-analyze"].graph == "issue-analyze"
    assert module.exports["issue-reconcile"].graph == "issue-reconcile"
    assert module.exports["report"].graph == "quality-report"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert _OWNED_GRAPHS == (
        "quality",
        "quality-fact-baseline",
        "quality-inspect",
        "quality-issue-triage",
        "quality-issue-analysis",
        "quality-report",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
    )
    assert _OWNERSHIP["owners"]["quality"]["exports"]["assess"]["target"] == "quality"
    assert _OWNERSHIP["owners"]["quality"]["exports"]["issue-review"]["target"] == "issue-review"
    assert _OWNERSHIP["owners"]["quality"]["exports"]["issue-analyze"]["target"] == "issue-analyze"
    assert _OWNERSHIP["owners"]["quality"]["exports"]["issue-reconcile"]["target"] == ("issue-reconcile")
    assert _OWNERSHIP["owners"]["quality"]["exports"]["report"]["target"] == "quality-report"

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
        f"{base}.{phase}": f"assurance.quality.agent.{base}.v1" for base in _BASES for phase in _PHASES
    }
    assert {name: slot.contract_id for name, slot in module.capability_slots.items()} == expected_slots
    assert len(module.capability_slots) == 15
    assert len(_BASES) == 5
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


def test_quality_feature_forbids_product_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["inspect"].__class__ is AgentExecutionContract


def test_inputs_carry_execution_evidence_artifact_and_budget_refs() -> None:
    for export, closure in (
        ("assess", "quality.assess"),
        ("issue-review", "quality.issue-review"),
        ("issue-analyze", "quality.issue-analyze"),
        ("issue-reconcile", "quality.issue-reconcile"),
        ("report", "quality.report"),
    ):
        pointers = {item["pointer"] for item in _OWNERSHIP["exported_closures"][closure]["root_pointers"]}
        assert pointers == _INVENTORY_POINTERS
        schema = json.loads(_schema(_io_schema_id(export, "input")).content)
        required = set(schema["required"])
        assert required == _INPUT_FIELDS
        assert schema["properties"]["execution_status"]["enum"] == ["failed", "passed"]
        budgets = schema["properties"]["budgets"]
        assert budgets["additionalProperties"] is False
        assert set(budgets["required"]) == {"coverage_rounds", "failure_rounds"}
        evidence = schema["properties"]["evidence_refs"]
        assert evidence["type"] == "array"
        assert set(evidence["items"]["required"]) == {"digest", "path"}


def test_every_quality_local_subgraph_call_projects_child_required_fields() -> None:
    module = _load_module()
    quality_calls = [item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS]
    assert {(item["graph"], item["node"], item["target"]) for item in quality_calls} == {
        ("quality", "fact-baseline", "quality-fact-baseline"),
        ("quality", "inspect", "quality-inspect"),
        ("issue-review", "review", "quality-issue-triage"),
        ("issue-analyze", "analyze", "quality-issue-analysis"),
        ("issue-reconcile", "analyze", "quality-issue-analysis"),
    }
    for item in quality_calls:
        caller = module.graphs[item["graph"]].nodes[item["node"]]
        assert caller.kind == "subgraph"
        assert caller.graph == item["target"]
        assert isinstance(caller.input_projection, ObjectProjection)
        required: set[str] = set()
        for _node_id, node in module.graphs[item["target"]].nodes.items():
            required.update(_graph_input_fields(node))
        assert set(caller.input_projection.fields) == required
        for field, projection in caller.input_projection.fields.items():
            assert isinstance(projection, GraphInputPointerProjection)
            assert projection.pointer == f"/{field}"


def test_assess_and_issue_wrappers_project_evidence_and_budget_refs() -> None:
    module = _load_module()
    hops = (
        ("assess", "quality", "fact-baseline", "quality-fact-baseline", _ASSESS_INPUT, _ISSUE_INPUT),
        ("assess", "quality", "inspect", "quality-inspect", _ASSESS_INPUT, _ISSUE_INPUT),
        (
            "issue-review",
            "issue-review",
            "review",
            "quality-issue-triage",
            _ISSUE_INPUT,
            _ASSESS_INPUT,
        ),
        (
            "issue-analyze",
            "issue-analyze",
            "analyze",
            "quality-issue-analysis",
            _ISSUE_INPUT,
            _REPORT_INPUT,
        ),
        (
            "issue-reconcile",
            "issue-reconcile",
            "analyze",
            "quality-issue-analysis",
            _ISSUE_INPUT,
            _REPORT_INPUT,
        ),
    )
    for export, caller_id, node_id, child_id, graph_input, foreign in hops:
        del export
        caller = module.graphs[caller_id]
        child = module.graphs[child_id]
        child_input = _as_mapping(
            project_task_input(
                _require_projection(caller.nodes[node_id].input_projection),
                root_input=foreign,
                graph_input=graph_input,
                node_config={},
                predecessor_tokens={},
            )
        )
        assert child_input["change_id"] == graph_input["change_id"]
        assert child_input["allowed_artifact_paths"] == graph_input["allowed_artifact_paths"]
        assert child_input["capability_leafs"] == graph_input["capability_leafs"]
        assert child_input["evidence_refs"] == graph_input["evidence_refs"]
        assert child_input["budgets"] == graph_input["budgets"]
        assert child_input["execution_status"] == graph_input["execution_status"]
        assert "leak_token" not in child_input
        assert set(child_input) == _INPUT_FIELDS
        assert set(_graph_input_fields(caller.nodes[node_id])) == _INPUT_FIELDS

        prepare = _as_mapping(
            project_task_input(
                _require_projection(child.nodes["prepare"].input_projection),
                root_input=foreign,
                graph_input=child_input,
                node_config={},
                predecessor_tokens={},
            )
        )
        assert prepare["change_id"] == graph_input["change_id"]
        assert prepare["artifact_paths"] == graph_input["allowed_artifact_paths"]
        assert prepare["evidence_refs"] == graph_input["evidence_refs"]
        assert prepare["budgets"] == graph_input["budgets"]
        assert prepare["execution_status"] == graph_input["execution_status"]
        assert "leak_token" not in prepare
        assert set(_graph_input_fields(child.nodes["prepare"])) == _INPUT_FIELDS


def test_quality_does_not_import_a_healing_workflow() -> None:
    module = _load_module()
    assert module.imports == {}
    assert set(module.graphs).isdisjoint(_HEALING_GRAPHS)
    dumped = json.dumps(module.model_dump(mode="json", by_alias=True), ensure_ascii=True)
    for graph_id in _HEALING_GRAPHS:
        assert graph_id not in dumped
    assert "assurance.healing" not in dumped
    raw = _module_resource().content.decode("utf-8")
    assert "assurance.healing" not in raw
    for graph_id in _HEALING_GRAPHS:
        assert graph_id not in raw


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    assert set(_LEAF_GRAPHS) == {
        f"quality-{base}" if base != "report" else "quality-report" for base in _BASES
    }
    for graph_id, base in _LEAF_GRAPHS.items():
        nodes = module.graphs[graph_id].nodes
        assert nodes["prepare"].capability == f"assurance.quality.{base}.prepare"
        assert nodes["prepare"].capability_slot is None
        assert nodes["finalize"].capability == f"assurance.quality.{base}.finalize"
        assert nodes["finalize"].capability_slot is None
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{base}.execute"


def test_public_outputs_expose_normalized_assessment_without_achieved() -> None:
    module = _load_module()
    issue_fields = {
        "change_id",
        "classification",
        "evidence_refs",
        "fix_eligible",
        "rounds_budget",
        "rounds_used",
    }
    for export in ("issue-review", "issue-analyze", "issue-reconcile"):
        projection = module.exports[export].output_projection
        assert isinstance(projection, OutputObjectProjection)
        assert set(projection.fields) == issue_fields
        for field in projection.fields.values():
            assert isinstance(field, ChildOutputPointerProjection)
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        assert schema["properties"]["classification"]["enum"] == list(_FAILURE_CLASSIFICATIONS)
        assert schema["properties"]["fix_eligible"]["type"] == "boolean"
        assert set(schema["properties"]) == issue_fields
        assert "achieved" not in json.dumps(schema)

    assess = module.exports["assess"].output_projection
    assert isinstance(assess, OutputObjectProjection)
    assert set(assess.fields) == {"change_id", "coverage_state", "evidence_refs"}
    for field in assess.fields.values():
        assert isinstance(field, ChildOutputPointerProjection)
    assess_schema = json.loads(_schema(_io_schema_id("assess", "output")).content)
    assert assess_schema["properties"]["coverage_state"]["enum"] == list(_COVERAGE_STATES)
    assert "achieved" not in json.dumps(assess_schema)

    report = module.exports["report"].output_projection
    assert isinstance(report, OutputObjectProjection)
    assert set(report.fields) == {"change_id", "report_refs"}
    for field in report.fields.values():
        assert isinstance(field, ChildOutputPointerProjection)
    report_schema = json.loads(_schema(_io_schema_id("report", "output")).content)
    assert set(report_schema["properties"]) == {"change_id", "report_refs"}
    assert "achieved" not in json.dumps(report_schema)
    assert "achieved" not in json.dumps(report.model_dump(mode="json"))
    dumped = json.dumps(module.exports["report"].model_dump(mode="json", by_alias=True))
    assert "achieved" not in dumped


def test_fake_slot_bindings_compile_each_export_without_an_agent_server() -> None:
    module = _load_module()
    for export, graph_id in _GRAPH_BY_EXPORT.items():
        compiled = _bind_and_compile(module, export)
        assert set(compiled.graphs) == set(_EXPORT_CLOSURES[export])
        payload = json.dumps(compiled.model_dump(mode="json", by_alias=True))
        assert "capability_slot" not in payload
        assert "assurance.product.agent." not in payload
        assert set(compiled.graphs).isdisjoint(set(module.graphs) - set(_EXPORT_CLOSURES[export]))
        assert set(compiled.graphs).isdisjoint(_HEALING_GRAPHS)
        for healing_graph in _HEALING_GRAPHS:
            assert healing_graph not in payload
