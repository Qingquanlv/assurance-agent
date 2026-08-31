from __future__ import annotations

import ast
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest
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
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    InvocationWorkspaceBinding,
    ResourceContribution,
    SchemaContribution,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import (
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed

from assurance_improvement.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_improvement.plugin import ImprovementPlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
)
_FEATURE_ROOT = Path(__file__).resolve().parents[1] / "assurance_improvement"
_MODULE_ID = "assurance.improvement.workflow"
_RESOURCE_ID = "assurance.improvement.workflow.module.v1"
_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"
_OWNED_GRAPHS = tuple(_OWNERSHIP["owners"]["improvement"]["graphs"])
_BASES = tuple(AGENT_JOB_CONTRACTS)
_PHASES = ("prepare", "execute", "finalize")
_EXPORTS = ("archive", "retro", "review", "evaluate", "export", "apply", "rollback")
_GRAPH_BY_EXPORT = {
    "archive": "archive",
    "retro": "retro",
    "review": "improvement-review",
    "evaluate": "improvement-evaluate",
    "export": "improvement-export",
    "apply": "improvement-apply",
    "rollback": "improvement-rollback",
}
_LEAF_GRAPHS = {
    "improvement-archive": "archive",
    "improvement-review": "improvement-review",
    "improvement-retro": "retro",
    "improvement-retro-eval-analysis": "retro-eval-analysis",
    "improvement-retro-issue-analysis": "retro-issue-analysis",
    "improvement-retro-workflow-analysis": "retro-workflow-analysis",
}
_EXPORT_CLOSURES = {
    "archive": ("archive", "improvement-archive"),
    "retro": (
        "improvement-retro",
        "improvement-retro-eval-analysis",
        "improvement-retro-issue-analysis",
        "improvement-retro-workflow-analysis",
        "retro",
    ),
    "review": ("improvement-review",),
    "evaluate": ("improvement-evaluate",),
    "export": ("improvement-export",),
    "apply": ("improvement-apply", "improvement-review"),
    "rollback": ("improvement-rollback",),
}
_FORBIDDEN_IMPORTS = (
    "assurance_product",
    "agent_runtime_opencode",
    "agent_runtime_cursor",
)
_PRODUCT_GRAPHS = tuple(_OWNERSHIP["owners"]["product"]["graphs"])
_LIFECYCLE_STATES = (
    "applied",
    "approved",
    "awaiting_baseline",
    "eval_error",
    "evaluating",
    "exported",
    "needs_rework",
    "proposed",
    "rejected",
    "rolled_back",
    "superseded",
)
_TERMINAL_OUTCOMES = ("applied", "failed", "needs_review", "rolled_back")
_INVENTORY_POINTERS = {"/allowed_artifact_paths", "/capability_leafs", "/change_id"}
_CHANGE_ID_POINTERS = {"/change_id"}
_INPUT_FIELDS = {
    "allowed_artifact_paths",
    "capability_leafs",
    "change_id",
    "evidence_refs",
    "lifecycle_state",
}
_ARCHIVE_INPUT = {
    "change_id": "CH-ARCHIVE-001",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "evidence_refs": [{"path": "qa/changes/CH-ARCHIVE-001/report/report.md", "digest": "a" * 64}],
    "lifecycle_state": "proposed",
    "leak_token": "must-not-cross-archive-boundary",
}
_RETRO_INPUT = {
    "change_id": "CH-RETRO-002",
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": ["qa/archive"],
    "evidence_refs": [{"path": "qa/archive/CH-RETRO-002/inspect/inspection.json", "digest": "b" * 64}],
    "lifecycle_state": "evaluating",
    "leak_token": "must-not-cross-retro-boundary",
}
_APPLY_INPUT = {
    "change_id": "CH-APPLY-003",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "evidence_refs": [{"path": "qa/changes/CH-APPLY-003/retro/retro.json", "digest": "c" * 64}],
    "lifecycle_state": "approved",
    "leak_token": "must-not-cross-apply-boundary",
}


class _FakeSlotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"status": "ok"})


def _contribution():
    return ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


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
                capability_id = f"test.improvement.slot.{slot}"
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
        name=f"improvement-{export}-isolation",
        entrypoints={export: _GRAPH_BY_EXPORT[export]},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    )
    return compile_workflow(workflow, _Capabilities())


def test_improvement_workflow_module_is_published() -> None:
    resource = _module_resource()
    assert resource.resource_id == _RESOURCE_ID
    assert resource.media_type == _MODULE_MIME

    module = parse_workflow_module(resource.content)
    assert module.role == "feature"
    assert module.owner_id == "assurance.improvement"
    assert module.module_id == _MODULE_ID
    assert module.module_version == "0.2.0"
    assert "name" not in module.model_fields_set
    assert module.name is None
    assert module.entrypoints == {}
    assert module.imports == {}
    assert tuple(module.exports) == _EXPORTS
    assert module.exports["archive"].graph == "archive"
    assert module.exports["retro"].graph == "retro"
    assert module.exports["review"].graph == "improvement-review"
    assert module.exports["evaluate"].graph == "improvement-evaluate"
    assert module.exports["export"].graph == "improvement-export"
    assert module.exports["apply"].graph == "improvement-apply"
    assert module.exports["rollback"].graph == "improvement-rollback"
    assert tuple(module.graphs) == _OWNED_GRAPHS
    assert _OWNED_GRAPHS == (
        "archive",
        "improvement-archive",
        "retro",
        "improvement-retro",
        "improvement-retro-eval-analysis",
        "improvement-retro-issue-analysis",
        "improvement-retro-workflow-analysis",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    )
    owners = _OWNERSHIP["owners"]["improvement"]["exports"]
    assert owners["archive"]["target"] == "archive"
    assert owners["retro"]["target"] == "retro"
    assert owners["review"]["target"] == "improvement-review"
    assert owners["evaluate"]["target"] == "improvement-evaluate"
    assert owners["export"]["target"] == "improvement-export"
    assert owners["apply"]["target"] == "improvement-apply"
    assert owners["rollback"]["target"] == "improvement-rollback"

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
        f"{base}.{phase}": f"assurance.improvement.agent.{base}.v1" for base in _BASES for phase in _PHASES
    }
    assert {name: slot.contract_id for name, slot in module.capability_slots.items()} == expected_slots
    assert len(module.capability_slots) == 18
    assert len(_BASES) == 6
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


def test_improvement_feature_forbids_product_and_adapter_imports() -> None:
    found: set[str] = set()
    for path in sorted(_FEATURE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(path):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS):
                found.add(module_name)
    assert found == set()
    from agent_runtime_contracts import AgentExecutionContract

    assert AGENT_JOB_CONTRACTS["archive"].__class__ is AgentExecutionContract


def test_inputs_carry_lifecycle_and_evidence_refs() -> None:
    pointer_sets = {
        "archive": _INVENTORY_POINTERS,
        "retro": _INVENTORY_POINTERS,
        "review": _INVENTORY_POINTERS,
        "evaluate": _CHANGE_ID_POINTERS,
        "export": _CHANGE_ID_POINTERS,
        "apply": _INVENTORY_POINTERS,
        "rollback": _CHANGE_ID_POINTERS,
    }
    for export, closure in (
        ("archive", "improvement.archive"),
        ("retro", "improvement.retro"),
        ("review", "improvement.review"),
        ("evaluate", "improvement.evaluate"),
        ("export", "improvement.export"),
        ("apply", "improvement.apply"),
        ("rollback", "improvement.rollback"),
    ):
        pointers = {item["pointer"] for item in _OWNERSHIP["exported_closures"][closure]["root_pointers"]}
        assert pointers == pointer_sets[export]
        schema = json.loads(_schema(_io_schema_id(export, "input")).content)
        required = set(schema["required"])
        assert required == _INPUT_FIELDS
        assert schema["properties"]["lifecycle_state"]["enum"] == list(_LIFECYCLE_STATES)
        evidence = schema["properties"]["evidence_refs"]
        assert evidence["type"] == "array"
        assert set(evidence["items"]["required"]) == {"digest", "path"}


def test_every_improvement_local_subgraph_call_projects_child_required_fields() -> None:
    module = _load_module()
    improvement_calls = [
        item for item in _OWNERSHIP["local_subgraph_calls"] if item["graph"] in _OWNED_GRAPHS
    ]
    assert {(item["graph"], item["node"], item["target"]) for item in improvement_calls} == {
        ("archive", "archive", "improvement-archive"),
        ("retro", "retro", "improvement-retro"),
        ("retro", "retro-eval-analysis", "improvement-retro-eval-analysis"),
        ("retro", "retro-issue-analysis", "improvement-retro-issue-analysis"),
        ("retro", "retro-workflow-analysis", "improvement-retro-workflow-analysis"),
        ("improvement-apply", "review", "improvement-review"),
    }
    for item in improvement_calls:
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


def test_archive_and_retro_project_lifecycle_evidence_through_two_private_levels() -> None:
    module = _load_module()
    hops = (
        ("archive", "archive", "archive", "improvement-archive", _ARCHIVE_INPUT, _RETRO_INPUT),
        ("retro", "retro", "retro", "improvement-retro", _RETRO_INPUT, _ARCHIVE_INPUT),
        (
            "retro",
            "retro",
            "retro-eval-analysis",
            "improvement-retro-eval-analysis",
            _RETRO_INPUT,
            _APPLY_INPUT,
        ),
        (
            "retro",
            "retro",
            "retro-issue-analysis",
            "improvement-retro-issue-analysis",
            _RETRO_INPUT,
            _APPLY_INPUT,
        ),
        (
            "retro",
            "retro",
            "retro-workflow-analysis",
            "improvement-retro-workflow-analysis",
            _RETRO_INPUT,
            _APPLY_INPUT,
        ),
        ("apply", "improvement-apply", "review", "improvement-review", _APPLY_INPUT, _ARCHIVE_INPUT),
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
        assert child_input["lifecycle_state"] == graph_input["lifecycle_state"]
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
        assert prepare["lifecycle_state"] == graph_input["lifecycle_state"]
        assert "leak_token" not in prepare
        assert set(_graph_input_fields(child.nodes["prepare"])) == _INPUT_FIELDS


def test_delivery_leaves_project_lifecycle_evidence_from_current_input() -> None:
    module = _load_module()
    leaves = (
        ("evaluate", "improvement-evaluate", "evaluate", _APPLY_INPUT, _RETRO_INPUT),
        ("export", "improvement-export", "export", _APPLY_INPUT, _ARCHIVE_INPUT),
        ("apply", "improvement-apply", "apply", _APPLY_INPUT, _RETRO_INPUT),
        ("rollback", "improvement-rollback", "rollback", _APPLY_INPUT, _ARCHIVE_INPUT),
        ("retro", "retro", "collect", _RETRO_INPUT, _ARCHIVE_INPUT),
        ("retro", "retro", "reconcile", _RETRO_INPUT, _ARCHIVE_INPUT),
    )
    for export, graph_id, node_id, graph_input, foreign in leaves:
        del export
        payload = _as_mapping(
            project_task_input(
                _require_projection(module.graphs[graph_id].nodes[node_id].input_projection),
                root_input=foreign,
                graph_input=graph_input,
                node_config={},
                predecessor_tokens={},
            )
        )
        assert payload["change_id"] == graph_input["change_id"]
        assert payload["evidence_refs"] == graph_input["evidence_refs"]
        assert payload["lifecycle_state"] == graph_input["lifecycle_state"]
        assert "leak_token" not in payload
        assert "decision" not in payload
        assert set(_graph_input_fields(module.graphs[graph_id].nodes[node_id])) == _INPUT_FIELDS


def test_execute_nodes_use_slots_and_prepare_finalize_keep_feature_ids() -> None:
    module = _load_module()
    assert set(_LEAF_GRAPHS) == {
        "improvement-archive",
        "improvement-review",
        "improvement-retro",
        "improvement-retro-eval-analysis",
        "improvement-retro-issue-analysis",
        "improvement-retro-workflow-analysis",
    }
    for graph_id, base in _LEAF_GRAPHS.items():
        nodes = module.graphs[graph_id].nodes
        assert nodes["prepare"].capability is None
        assert nodes["prepare"].capability_slot == f"{base}.prepare"
        assert nodes["finalize"].capability is None
        assert nodes["finalize"].capability_slot == f"{base}.finalize"
        assert nodes["execute"].capability is None
        assert nodes["execute"].capability_slot == f"{base}.execute"


def test_deterministic_operations_and_effects_stay_concrete() -> None:
    module = _load_module()
    concrete = {
        ("retro", "collect"): "assurance.improvement.retro-collect-v3",
        ("retro", "reconcile"): "assurance.improvement.reconcile-improvements",
        ("improvement-evaluate", "evaluate"): "assurance.improvement.evaluate-memory-improvement",
        ("improvement-export", "export"): "assurance.improvement.export-change-improvement",
        ("improvement-apply", "apply"): "assurance.improvement.apply-memory-improvement",
        ("improvement-rollback", "rollback"): "assurance.improvement.rollback-memory-improvement",
    }
    for (graph_id, node_id), capability in concrete.items():
        node = module.graphs[graph_id].nodes[node_id]
        assert node.kind == "task"
        assert node.capability == capability
        assert node.capability_slot is None
    assert not module.effects
    from assurance_improvement.plugin import ImprovementPlugin

    assert ImprovementPlugin.descriptor().effects == (
        "assurance.improvement.effect.archive.v1",
        "assurance.improvement.effect.delivery.v1",
        "assurance.improvement.effect.promotion.v1",
    )


def test_public_outputs_expose_lifecycle_receipts_and_outcome_without_review_tokens() -> None:
    module = _load_module()
    lifecycle_fields = {"change_id", "evidence_refs", "lifecycle_state"}
    receipt_fields = {"change_id", "lifecycle_state", "receipt_refs"}
    delivery_fields = {"change_id", "lifecycle_state", "outcome", "receipt_refs"}

    for export, fields in (
        ("archive", lifecycle_fields),
        ("retro", lifecycle_fields),
        ("review", lifecycle_fields),
        ("evaluate", receipt_fields),
        ("export", receipt_fields),
        ("apply", delivery_fields),
        ("rollback", delivery_fields),
    ):
        projection = module.exports[export].output_projection
        assert isinstance(projection, OutputObjectProjection)
        assert set(projection.fields) == fields
        for field in projection.fields.values():
            assert isinstance(field, ChildOutputPointerProjection)
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        assert set(schema["properties"]) == fields
        assert schema["properties"]["lifecycle_state"]["enum"] == list(_LIFECYCLE_STATES)
        dumped = json.dumps(schema)
        assert "decision" not in dumped
        assert "review_token" not in dumped
        assert "decision" not in json.dumps(projection.model_dump(mode="json"))

    for export in ("evaluate", "export", "apply", "rollback"):
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        receipts = schema["properties"]["receipt_refs"]
        assert receipts["type"] == "array"
        assert set(receipts["items"]["required"]) == {"digest", "kind"}

    for export in ("apply", "rollback"):
        schema = json.loads(_schema(_io_schema_id(export, "output")).content)
        assert schema["properties"]["outcome"]["enum"] == list(_TERMINAL_OUTCOMES)


def test_apply_review_and_evaluation_routes_are_exclusive() -> None:
    module = _load_module()
    apply_graph = module.graphs["improvement-apply"]
    assert apply_graph.start == "review"
    review = apply_graph.nodes["review"]
    assert review.kind == "subgraph"
    assert review.graph == "improvement-review"
    human = apply_graph.nodes["human-review"]
    assert human.kind == "interrupt"
    assert human.reason == "needs_human_review"
    assert tuple(human.actions) == ("approve", "reject", "request_rework", "supersede")
    assert human.routing is not None
    assert human.routing.mode == "exclusive"
    evaluate = apply_graph.nodes["evaluate"]
    assert evaluate.routing is not None
    assert evaluate.routing.mode == "exclusive"
    assert apply_graph.nodes["apply-auto-review"].routing is not None
    assert apply_graph.nodes["apply-auto-review"].routing.mode == "exclusive"
    assert apply_graph.nodes["apply-auto-review"].capability == (
        "assurance.improvement.apply-improvement-auto-review"
    )
    assert apply_graph.nodes["apply-human-review"].capability == (
        "assurance.improvement.apply-improvement-review"
    )
    assert apply_graph.nodes["evaluate"].capability == ("assurance.improvement.evaluate-memory-improvement")
    assert "review-pass-gate" not in apply_graph.nodes
    assert "review-human-gate" not in apply_graph.nodes
    apply_output = json.dumps(module.exports["apply"].model_dump(mode="json", by_alias=True))
    assert "decision" not in apply_output
    assert "/decision" not in apply_output
    otherwise = [edge for edge in apply_graph.edges if edge.otherwise]
    assert {edge.from_ for edge in otherwise} == {
        "human-review",
        "apply-auto-review",
        "apply-human-review",
        "evaluate",
    }
    assert all(edge.to == "failed" for edge in otherwise)


def test_fake_slot_bindings_compile_each_export_without_an_agent_server() -> None:
    module = _load_module()
    assert set(module.graphs).isdisjoint(_PRODUCT_GRAPHS)
    for export, graph_id in _GRAPH_BY_EXPORT.items():
        compiled = _bind_and_compile(module, export)
        assert set(compiled.graphs) == set(_EXPORT_CLOSURES[export])
        payload = json.dumps(compiled.model_dump(mode="json", by_alias=True))
        assert "capability_slot" not in payload
        assert "assurance.product.agent." not in payload
        assert set(compiled.graphs).isdisjoint(set(module.graphs) - set(_EXPORT_CLOSURES[export]))
        assert set(compiled.graphs).isdisjoint(_PRODUCT_GRAPHS)
        assert graph_id in compiled.graphs


@dataclass(frozen=True)
class ApplyDriveResult:
    status: str
    ends: frozenset[str]
    applied: bool
    capabilities: tuple[str, ...]


class _ScriptedApplyHost:
    def __init__(self, *, decision: str, evaluation: str = "passed") -> None:
        self._decision = decision
        self._evaluation = evaluation

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        request_input = call.request.input if isinstance(call.request.input, Mapping) else {}
        if capability_id.endswith("improvement-review.finalize"):
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded({"decision": self._decision, "lifecycle_state": "proposed"}),
            )
        if capability_id.endswith("apply-improvement-auto-review"):
            lifecycle = {
                "pass": "approved",
                "changes_requested": "needs_rework",
                "reject": "rejected",
            }.get(self._decision, "proposed")
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded(
                    {
                        "lifecycle_state": lifecycle,
                        "decision": request_input.get("decision", self._decision),
                        "effect_intents": [],
                        "write_authorization": [],
                    }
                ),
            )
        if capability_id.endswith("apply-improvement-review"):
            action = request_input.get("action") or request_input.get("decision")
            lifecycle = {
                "approve": "approved",
                "reject": "rejected",
                "request_rework": "needs_rework",
                "supersede": "superseded",
            }.get(str(action), "proposed")
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded(
                    {
                        "lifecycle_state": lifecycle,
                        "action": action,
                        "effect_intents": [],
                        "write_authorization": [],
                    }
                ),
            )
        if capability_id.endswith("evaluate-memory-improvement"):
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded({"outcome": self._evaluation, "lifecycle_state": "evaluating"}),
            )
        if capability_id.endswith("apply-memory-improvement"):
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded({"outcome": "applied", "lifecycle_state": "applied"}),
            )
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.succeeded(
                {
                    "change_id": "CH-APPLY-003",
                    "decision": self._decision,
                    "lifecycle_state": "proposed",
                    "evidence_refs": [],
                }
            ),
        )

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="indeterminate", reason="scripted host"),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="indeterminate", reason="scripted host"),
        )

    def read_terminal_receipts(self, identity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


def _drive_node_payload(node: NodeDef) -> dict[str, object]:
    payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
    slot = payload.pop("capability_slot", None)
    if slot is not None:
        payload["capability"] = f"assurance.improvement.slot.{slot}"
    return payload


def _drive_apply(
    *,
    decision: str,
    evaluation: str = "passed",
    resumes: tuple[str, ...] = (),
) -> ApplyDriveResult:
    from tests.product.runtime_composition import resolve_workflow_composition

    module = _load_module()
    workflow = WorkflowDef(
        name="improvement-apply-drive",
        entrypoints={"apply": "improvement-apply"},
        retry=module.retry,
        timeout=module.timeout,
        graphs={
            graph_id: module.graphs[graph_id].model_copy(
                update={
                    "nodes": {
                        node_id: NodeDef.model_validate(_drive_node_payload(node))
                        for node_id, node in module.graphs[graph_id].nodes.items()
                    }
                }
            )
            for graph_id in _EXPORT_CLOSURES["apply"]
        },
    )
    handlers = {
        node.capability: _FakeSlotHandler()
        for graph in workflow.graphs.values()
        for node in graph.nodes.values()
        if node.capability
    }
    composition = resolve_workflow_composition(
        workflow.model_dump(mode="json", by_alias=True, exclude_unset=True),
        handlers,
    )
    host = _ScriptedApplyHost(decision=decision, evaluation=evaluation)
    with TemporaryDirectory(prefix="improvement-apply-drive-") as raw:
        root = Path(raw).resolve()
        for path in ("project", "attempts", "receipts", "runtime"):
            (root / path).mkdir()
        (root / "runtime" / "invocations").mkdir()
        engine = Engine(root / "runtime", host=host)
        try:
            handle = engine.start(
                composition,
                entrypoint="apply",
                invocation_id=f"apply-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, dict(_APPLY_INPUT))),
                authorization=empty_runtime_authorization(),
                workspace_binding=InvocationWorkspaceBinding(
                    project_root=root / "project",
                    attempts_root=root / "attempts",
                    receipts_root=root / "receipts",
                ),
            )
            result = engine.run_until_blocked(handle)
            for action in resumes:
                handle = engine.resume(handle, action=action, payload={"decision": action})
                result = engine.run_until_blocked(handle)
            ends = frozenset(
                activation.node_id
                for activation in result.projection.activations
                if activation.node_id in {"done", "failed", "rejected", "rework", "superseded"}
                and activation.status == "completed"
            )
            capabilities = tuple(
                str(getattr(activation, "capability_id", "") or activation.node_id)
                for activation in result.projection.activations
                if activation.status == "completed"
            )
            applied = any(
                activation.node_id == "apply" and activation.status == "completed"
                for activation in result.projection.activations
            )
            return ApplyDriveResult(
                status=result.status,
                ends=ends,
                applied=applied,
                capabilities=capabilities,
            )
        finally:
            engine.close()


@pytest.mark.parametrize(
    ("decision", "end", "applied"),
    (
        ("pass", "done", True),
        ("changes_requested", "rework", False),
        ("reject", "rejected", False),
        ("needs_human_review", "failed", False),
    ),
)
def test_apply_auto_review_routes_are_exclusive(decision: str, end: str, applied: bool) -> None:
    result = _drive_apply(decision=decision)
    if decision == "needs_human_review":
        assert result.status == "interrupted"
        assert result.applied is False
        return
    assert result.status == "succeeded"
    assert end in result.ends
    assert result.applied is applied


@pytest.mark.parametrize(
    ("action", "end"),
    (("reject", "rejected"), ("request_rework", "rework"), ("supersede", "superseded")),
)
def test_human_non_approve_actions_do_not_apply(action: str, end: str) -> None:
    result = _drive_apply(decision="needs_human_review", resumes=(action,))
    assert result.status == "succeeded"
    assert end in result.ends
    assert result.applied is False


def test_human_approve_applies_only_after_passed_evaluation() -> None:
    passed = _drive_apply(decision="needs_human_review", evaluation="passed", resumes=("approve",))
    failed = _drive_apply(decision="needs_human_review", evaluation="regressed", resumes=("approve",))
    assert passed.applied is True
    assert "done" in passed.ends
    assert failed.applied is False
    assert "failed" in failed.ends
