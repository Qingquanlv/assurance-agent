from __future__ import annotations

import ast
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest
import yaml

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue
from graph_engine.graph import compile_workflow, parse_workflow_module, project_task_input
from graph_engine.graph.input_projection import (
    GraphInputPointerProjection,
    InputProjectionDef,
    LiteralProjection,
    ObjectProjection,
    PredecessorPointerProjection,
    RootPointerProjection,
)
from graph_engine.graph.module_schema import WorkflowModuleDef
from graph_engine.graph.output_projection import (
    ChildOutputPointerProjection,
    ObjectProjection as OutputObjectProjection,
)
from graph_engine.graph.schema import EdgeDef, NodeDef
from graph_engine.plugin_api import (
    InvocationWorkspaceBinding,
    ResourceContribution,
    SchemaContribution,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskHandler,
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
from graph_engine.runtime.models import InvocationProjection
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.json_schema import validate_json_schema
from graph_engine.runtime.seed import empty_invocation_seed

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families

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
    assert module.module_version == "0.2.0"
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
    assert "uniqueItems" not in families
    assert families["items"]["enum"] == list(_FAMILIES)
    validate_selected_families(tuple(_FAMILIES))


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
        counter_fields = {"rounds_used", "rounds_budget"}
        for field, projection in caller.input_projection.fields.items():
            if field in counter_fields:
                assert isinstance(projection, LiteralProjection | PredecessorPointerProjection)
                continue
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
        leaf = {key: child_input[key] for key in ("change_id", "allowed_artifact_paths", "capability_leafs")}
        hop_inputs[node_id] = leaf

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
        selector = root.nodes[f"select-{family}"]
        assert selector.routing is not None
        assert selector.routing.mode == "exclusive"
        assert (f"select-{family}", family, "output.value == true") in actual_edges
        otherwise = [
            edge for edge in root.edges if edge.from_ == f"select-{family}" and edge.to == f"{family}-skip"
        ]
        assert len(otherwise) == 1
        assert otherwise[0].otherwise is True
        assert otherwise[0].condition is None
        assert (family, f"{family}-done", None) in actual_edges
        assert (f"{family}-skip", f"{family}-done", None) in actual_edges
        assert (f"{family}-done", "join-selected", None) in actual_edges
    assert ("join-selected", "done", None) in actual_edges
    assert root.nodes["fanout"].routing is not None
    assert root.nodes["fanout"].routing.mode == "fanout"
    assert root.nodes["fanout"].routing.min_matches == 4


def test_review_and_codegen_outcomes_use_exclusive_routes() -> None:
    module = _load_module()
    for family, has_fix in (("api", True), ("e2e", True), ("fuzz", False), ("performance", False)):
        lane = module.graphs[f"generation-{family}"]
        review = lane.nodes["plan-review"]
        assert review.routing is not None
        assert review.routing.mode == "exclusive"
        human = lane.nodes["plan-human-review"]
        assert human.kind == "interrupt"
        assert tuple(human.actions) == ("approve", "reject", "request_rework")
        assert human.routing is not None
        assert human.routing.mode == "exclusive"
        assert lane.nodes["plan-review-round-advance"].capability == (
            "assurance.generation.review-round.advance"
        )
        assert lane.nodes["plan-review-round-advance"].routing is not None
        assert lane.nodes["plan-review-round-advance"].routing.mode == "exclusive"
        assert "rejected" in lane.nodes
        assert "exhausted" in lane.nodes
        assert "plan-review-pass-gate" not in lane.nodes
        if has_fix:
            codegen = lane.nodes["codegen"]
            assert codegen.routing is not None
            assert codegen.routing.mode == "exclusive"
            assert "codegen-fix" in lane.nodes
            assert lane.nodes["codegen-round-advance"].capability == (
                "assurance.generation.review-round.advance"
            )
        else:
            assert "codegen-fix" not in lane.nodes
            assert "codegen-round-advance" not in lane.nodes


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


_REVIEW_ADVANCE = "assurance.generation.review-round.advance"
_PUBLIC_DIGEST = "a" * 64


@dataclass(frozen=True)
class GenerationDriveResult:
    status: str
    selected: tuple[str, ...]
    dispatched_families: frozenset[str]
    skip_families: frozenset[str]
    join_token_count: int
    advance_count: int
    counters: tuple[int, ...]
    end_nodes: frozenset[str]
    public_outcome: str


class _ScriptedGenerationHost:
    def __init__(
        self,
        *,
        reviews: tuple[str, ...] = ("pass",),
        codegen_verdicts: tuple[str, ...] = ("accepted",),
        pass_readiness: str = "ready",
    ) -> None:
        self._reviews = reviews
        self._codegen = codegen_verdicts
        self._pass_readiness = pass_readiness
        self._review_index = 0
        self._review_index_by_family: dict[str, int] = {}
        self._codegen_index = 0
        self._codegen_index_by_family: dict[str, int] = {}
        self._advance = None
        self.advance_outputs: list[dict[str, object]] = []

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        if capability_id == _REVIEW_ADVANCE or capability_id.endswith("review-round.advance"):
            if self._advance is None:
                from assurance_generation.operations.workflow_state import (
                    GenerationReviewRoundAdvanceHandler,
                )

                self._advance = GenerationReviewRoundAdvanceHandler()
            context = TaskContext(
                project_root=Path.cwd(),
                write_root=Path.cwd(),
                workspace_identity=call.attempt_root.workspace_identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            )
            outcome = await self._advance.execute(call.request, context)
            if isinstance(outcome.output, Mapping):
                self.advance_outputs.append(dict(outcome.output))
            return TaskHostCallResult(operation="execute", outcome=outcome)
        request_input = call.request.input
        change_id = "CH-DEMO-001"
        rounds_used = 0
        rounds_budget = 2
        if isinstance(request_input, Mapping):
            if isinstance(request_input.get("change_id"), str):
                change_id = request_input["change_id"]
            if isinstance(request_input.get("rounds_used"), int):
                rounds_used = request_input["rounds_used"]
            if isinstance(request_input.get("rounds_budget"), int):
                rounds_budget = request_input["rounds_budget"]
        if capability_id.endswith("plan-review.finalize"):
            family = capability_id.split(".")[2] if capability_id.count(".") >= 3 else "api"
            index = self._review_index_by_family.get(family, 0)
            decision = self._reviews[min(index, len(self._reviews) - 1)]
            self._review_index_by_family[family] = index + 1
            self._review_index += 1
            fixable = decision in {"needs_fix", "changes_requested"}
            human = decision in {"needs_human_review"}
            if decision == "reject":
                fixable = False
                human = False
            public = {
                "pass": "pass",
                "approved": "pass",
                "needs_fix": "needs_fix",
                "changes_requested": "needs_fix",
                "needs_human_review": "needs_human",
                "reject": "reject",
            }[decision]
            if fixable:
                public = "needs_fix"
            elif human:
                public = "needs_human"
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded(
                    {
                        "auto_fix_allowed": fixable,
                        "auto_fix_plan": ["FIX-1"] if fixable else [],
                        "change_id": change_id,
                        "codegen_readiness": self._pass_readiness if public == "pass" else "not_ready",
                        "decision": decision,
                        "human_review_required": human,
                        "public_outcome": public,
                        "rounds_budget": rounds_budget,
                        "rounds_used": rounds_used,
                    }
                ),
            )
        if capability_id.endswith("codegen.finalize"):
            family = capability_id.split(".")[2] if capability_id.count(".") >= 3 else "api"
            index = self._codegen_index_by_family.get(family, 0)
            verdict = self._codegen[min(index, len(self._codegen) - 1)]
            self._codegen_index_by_family[family] = index + 1
            self._codegen_index += 1
            if family in {"api", "e2e"}:
                output: dict[str, object] = {
                    "change_id": change_id,
                    "schema_version": "2",
                    "verdict": verdict,
                }
                if verdict == "needs_fix":
                    output["repair"] = {
                        "allowed_paths": ["tests/api/test_users.py"],
                        "summary": "repair generated assertion",
                    }
            else:
                output = {"change_id": change_id, "needs_fix": False, "schema_version": "1"}
            return TaskHostCallResult(
                operation="execute", outcome=TaskOutcome.succeeded(cast(JSONValue, output))
            )
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.succeeded(
                {
                    "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
                    "change_id": change_id,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
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


def _compile_generation_workflow():
    module = _load_module()
    handlers: dict[str, TaskHandler] = {}
    graphs = {}
    for graph_id, graph in module.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            slot = payload.pop("capability_slot", None)
            if slot is not None:
                capability_id = f"assurance.generation.slot.{slot}"
                payload["capability"] = capability_id
                handlers[capability_id] = _FakeSlotHandler()
            elif node.capability is not None:
                handlers[node.capability] = _FakeSlotHandler()
            nodes[node_id] = NodeDef.model_validate(payload)
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})
    from graph_engine.graph.schema import WorkflowDef

    return WorkflowDef(
        name="generation-isolation",
        entrypoints={"generate": "generation"},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    ), handlers


def _lane_end_nodes(projection: InvocationProjection, family: str) -> frozenset[str]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    ends: set[str] = set()
    lane_id = f"generation-{family}"
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        if graph.graph_id == lane_id or graph.graph_id.endswith(f".{lane_id}"):
            if activation.node_id in {"done", "rejected", "exhausted"} and activation.status == "completed":
                ends.add(activation.node_id)
    return frozenset(ends)


def _drive_generate(
    *,
    selected: tuple[str, ...],
    reviews: tuple[str, ...] = ("pass",),
    codegen_verdicts: tuple[str, ...] = ("accepted",),
    resumes: tuple[str, ...] = (),
    pass_readiness: str = "ready",
) -> GenerationDriveResult:
    from tests.product.runtime_composition import resolve_workflow_composition

    validate_selected_families(selected)
    validate_json_schema(
        {
            "allowed_artifact_paths": _PUBLIC_INPUT["allowed_artifact_paths"],
            "capability_leafs": _PUBLIC_INPUT["capability_leafs"],
            "change_id": _PUBLIC_INPUT["change_id"],
            "selected_test_families": list(selected),
        },
        _schema(_io_schema_id("generate", "input")).content,
    )
    workflow, handlers = _compile_generation_workflow()
    document = workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)
    composition = resolve_workflow_composition(document, handlers)
    host = _ScriptedGenerationHost(
        reviews=reviews, codegen_verdicts=codegen_verdicts, pass_readiness=pass_readiness
    )
    root_input = {
        **_PUBLIC_INPUT,
        "selected_test_families": list(selected),
    }
    with TemporaryDirectory(prefix="generation-drive-") as raw:
        root = Path(raw).resolve()
        project = root / "project"
        attempts = root / "attempts"
        receipts = root / "receipts"
        runtime = root / "runtime"
        for path in (project, attempts, receipts, runtime):
            path.mkdir()
        (runtime / "invocations").mkdir()
        engine = Engine(runtime, host=host)
        try:
            handle = engine.start(
                composition,
                entrypoint="generate",
                invocation_id=f"generation-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, root_input)),
                authorization=empty_runtime_authorization(),
                workspace_binding=InvocationWorkspaceBinding(
                    project_root=project,
                    attempts_root=attempts,
                    receipts_root=receipts,
                ),
            )
            result = engine.run_until_blocked(handle)
            for action in resumes:
                if result.status != "interrupted":
                    break
                handle = engine.resume(handle, action=action, payload={"decision": action})
                result = engine.run_until_blocked(handle)
            projection = result.projection
            graphs = {item.graph_instance_id: item for item in projection.graph_instances}
            dispatched: set[str] = set()
            skipped: set[str] = set()
            join_tokens = 0
            for activation in projection.activations:
                graph = graphs[activation.graph_instance_id]
                if graph.graph_id.endswith(".generation") or graph.graph_id == "generation":
                    if activation.node_id in _FAMILIES and activation.status == "completed":
                        dispatched.add(activation.node_id)
                    if activation.node_id.endswith("-skip") and activation.status == "completed":
                        skipped.add(activation.node_id.removesuffix("-skip"))
                    if activation.node_id == "join-selected" and activation.status == "completed":
                        join_tokens = len(activation.token_ids)
            ends: set[str] = set()
            for family in selected:
                ends.update(_lane_end_nodes(projection, family))
            public = "passed"
            if ends == {"rejected"}:
                public = "rejected"
            elif "exhausted" in ends:
                public = "exhausted"
            return GenerationDriveResult(
                status=result.status,
                selected=selected,
                dispatched_families=frozenset(dispatched),
                skip_families=frozenset(skipped),
                join_token_count=join_tokens,
                advance_count=len(host.advance_outputs),
                counters=tuple(int(cast(int, item["rounds_used"])) for item in host.advance_outputs),
                end_nodes=frozenset(ends),
                public_outcome=public,
            )
        finally:
            engine.close()


def _all_nonempty_subsets() -> tuple[tuple[str, ...], ...]:
    return tuple(combo for size in range(1, 5) for combo in combinations(GENERATION_FAMILIES, size))


@pytest.mark.parametrize("selected", _all_nonempty_subsets())
def test_each_selected_family_dispatches_once_and_unselected_emit_skip_tokens(
    selected: tuple[str, ...],
) -> None:
    result = _drive_generate(selected=selected)
    assert result.dispatched_families == frozenset(selected)
    assert result.skip_families == frozenset(GENERATION_FAMILIES) - frozenset(selected)
    assert result.join_token_count == 4
    assert result.status == "succeeded"


@pytest.mark.parametrize("selected", [(), ("api", "api"), ("api", "mobile")])
def test_empty_duplicate_and_unknown_families_fail_at_feature_input(
    selected: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        validate_selected_families(selected)
    with pytest.raises(Exception):
        _drive_generate(selected=selected)


@pytest.mark.parametrize("family", _FAMILIES)
def test_plan_review_pass_completes_without_advance(family: str) -> None:
    result = _drive_generate(selected=(family,), reviews=("pass",))
    assert result.public_outcome == "passed"
    assert result.advance_count == 0
    assert result.end_nodes == {"done"}


@pytest.mark.parametrize("family", _FAMILIES)
def test_plan_review_auto_fix_advances_once_per_loop(family: str) -> None:
    result = _drive_generate(selected=(family,), reviews=("needs_fix", "pass"))
    assert result.public_outcome == "passed"
    assert result.advance_count == 1
    assert result.counters == (1,)


@pytest.mark.parametrize("family", _FAMILIES)
def test_plan_review_budget_exhaustion_is_explicit(family: str) -> None:
    result = _drive_generate(selected=(family,), reviews=("needs_fix", "needs_fix", "needs_fix"))
    assert result.public_outcome == "exhausted"
    assert result.advance_count == 2
    assert result.counters == (1, 2)
    assert result.status == "succeeded"


@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", "passed"), ("reject", "rejected"), ("request_rework", "passed")],
)
@pytest.mark.parametrize("family", _FAMILIES)
def test_plan_review_human_action_has_one_successor(family: str, action: str, expected: str) -> None:
    reviews = ("needs_human_review", "pass") if action == "request_rework" else ("needs_human_review",)
    result = _drive_generate(selected=(family,), reviews=reviews, resumes=(action,))
    assert result.public_outcome == expected
    if action == "request_rework":
        assert result.advance_count == 1
        assert result.counters == (1,)
    else:
        assert result.advance_count == 0


@pytest.mark.parametrize("family", ("api", "e2e"))
def test_api_e2e_codegen_needs_fix_reaches_fixer(family: str) -> None:
    result = _drive_generate(
        selected=(family,),
        reviews=("pass",),
        codegen_verdicts=("needs_fix",),
    )
    assert result.status == "succeeded"
    assert result.advance_count == 1
    assert result.counters == (1,)
    assert result.end_nodes == {"done"}


_FAMILY_TERMINALS = ("api-done", "e2e-done", "fuzz-done", "performance-done")


def _project_join_selected(
    projection: InvocationProjection,
    *,
    order: tuple[str, ...],
    graph_input: Mapping[str, object],
) -> Mapping[str, JSONValue]:
    module = _load_module()
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    join = next(
        activation
        for activation in projection.activations
        if activation.status == "completed"
        and activation.node_id == "join-selected"
        and (
            graphs[activation.graph_instance_id].graph_id == "generation"
            or graphs[activation.graph_instance_id].graph_id.endswith(".generation")
        )
    )
    tokens = {item.token_id: item for item in projection.offered_tokens}
    raw: dict[str, object] = {}
    for token_id in join.token_ids:
        token = tokens[token_id]
        if token.source is not None:
            raw[token.source] = thaw_json(token.payload)
    missing = [source for source in order if source not in raw]
    extra = sorted(set(raw) - set(order))
    assert not missing and not extra, f"join predecessors missing={missing} extra={extra}"
    predecessor_tokens = {source: raw[source] for source in order}
    compiled = module.graphs["generation"].nodes["join-selected"]
    assert compiled.input_projection is not None
    return cast(
        Mapping[str, JSONValue],
        freeze_json(
            project_task_input(
                compiled.input_projection,
                root_input=graph_input,
                graph_input=graph_input,
                node_config={},
                predecessor_tokens=predecessor_tokens,
            )
        ),
    )


def _drive_generate_projection(
    *,
    selected: tuple[str, ...],
    reviews: tuple[str, ...] = ("pass",),
) -> InvocationProjection:
    from tests.product.runtime_composition import resolve_workflow_composition

    validate_selected_families(selected)
    workflow, handlers = _compile_generation_workflow()
    composition = resolve_workflow_composition(
        workflow.model_dump(mode="json", by_alias=True, exclude_unset=True),
        handlers,
    )
    host = _ScriptedGenerationHost(reviews=reviews)
    root_input = {**_PUBLIC_INPUT, "selected_test_families": list(selected)}
    with TemporaryDirectory(prefix="generation-join-") as raw:
        root = Path(raw).resolve()
        project = root / "project"
        attempts = root / "attempts"
        receipts = root / "receipts"
        runtime = root / "runtime"
        for path in (project, attempts, receipts, runtime):
            path.mkdir()
        (runtime / "invocations").mkdir()
        engine = Engine(runtime, host=host)
        try:
            handle = engine.start(
                composition,
                entrypoint="generate",
                invocation_id=f"generation-join-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, root_input)),
                authorization=empty_runtime_authorization(),
                workspace_binding=InvocationWorkspaceBinding(
                    project_root=project,
                    attempts_root=attempts,
                    receipts_root=receipts,
                ),
            )
            return engine.run_until_blocked(handle).projection
        finally:
            engine.close()


def test_join_is_order_independent_for_all_four_lanes() -> None:
    graph_input = {**_PUBLIC_INPUT, "selected_test_families": list(GENERATION_FAMILIES)}
    projection = _drive_generate_projection(selected=GENERATION_FAMILIES)
    forward = _project_join_selected(projection, order=_FAMILY_TERMINALS, graph_input=graph_input)
    reverse = _project_join_selected(
        projection, order=tuple(reversed(_FAMILY_TERMINALS)), graph_input=graph_input
    )
    assert forward == reverse
    assert list(cast(list[str], forward["selected_families"])) == list(GENERATION_FAMILIES)
    result = _drive_generate(selected=GENERATION_FAMILIES)
    assert result.join_token_count == 4
    assert result.dispatched_families == frozenset(GENERATION_FAMILIES)


def test_two_lane_completions_in_scheduler_order_share_counters() -> None:
    first = _drive_generate(selected=("api", "e2e"), reviews=("needs_fix", "pass"))
    second = _drive_generate(selected=("e2e", "api"), reviews=("needs_fix", "pass"))
    assert first.counters == second.counters == (1, 1)
    graph_input = {**_PUBLIC_INPUT, "selected_test_families": ["api", "e2e"]}
    projection = _drive_generate_projection(selected=("api", "e2e"), reviews=("needs_fix", "pass"))
    forward = _project_join_selected(projection, order=_FAMILY_TERMINALS, graph_input=graph_input)
    reverse = _project_join_selected(
        projection, order=tuple(reversed(_FAMILY_TERMINALS)), graph_input=graph_input
    )
    assert forward == reverse


def test_not_ready_pass_does_not_enter_codegen() -> None:
    result = _drive_generate(selected=("api",), reviews=("pass",), pass_readiness="not_ready")
    assert result.end_nodes == {"exhausted"}
    assert result.advance_count == 0
    assert result.public_outcome == "exhausted"
