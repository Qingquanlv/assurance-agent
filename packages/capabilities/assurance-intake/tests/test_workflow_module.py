from __future__ import annotations

import ast
import json
import uuid

import pytest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import yaml

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue
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
    InvocationWorkspaceBinding,
    ResourceContribution,
    SchemaContribution,
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
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
)

from assurance_intake.contracts.workflow import AGENT_JOB_CONTRACTS
from assurance_intake.plugin import IntakePlugin

_WORKTREE = Path(__file__).resolve().parents[4]
_OWNERSHIP = yaml.safe_load(
    (_WORKTREE / "tests/product/fixtures/workflow-module-ownership.yaml").read_text(encoding="utf-8")
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
    "budgets": {"coverage_rounds": 2, "review_rounds": 2},
    "leak_token": "must-not-cross-subgraph-boundary",
}
_PUBLIC_DIGEST = "a" * 64
_REVIEW_ADVANCE = "assurance.intake.review-round.advance"


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
    assert module.module_version == "0.2.0"
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
    for node_id in ("intake", "explore", "case-design", "case-review", "human-review"):
        assert node_id in entry.nodes
    assert "generation" not in entry.nodes
    assert "done" in entry.nodes
    assert entry.nodes["done"].kind == "end"
    assert "rejected" in entry.nodes
    assert "exhausted" in entry.nodes
    assert "review-pass-gate" not in entry.nodes
    assert "review-fix-gate" not in entry.nodes
    assert "review-human-gate" not in entry.nodes
    human = entry.nodes["human-review"]
    assert human.kind == "interrupt"
    assert human.reason == "needs_human_review"
    assert tuple(human.actions) == ("approve", "reject", "request_rework")
    assert ("case-review", "done", None) not in {_edge_record(edge) for edge in entry.edges}
    assert all(edge.to != "generation" for edge in entry.edges)
    review = entry.nodes["case-review"]
    assert review.routing is not None
    assert review.routing.mode == "exclusive"


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
        public_required = required - {"rounds_used", "rounds_budget"}
        assert public_required <= set(caller.input_projection.fields)
        for field in public_required:
            projection = caller.input_projection.fields[field]
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
        assert {"decision", "artifacts"} <= set(schema["properties"])
        assert set(schema["required"]) == {"decision", "artifacts"}


def test_fake_slot_bindings_compile_without_an_agent_server() -> None:
    module = _load_module()
    handlers: dict[str, TaskHandler] = {}
    graphs = {}
    for graph_id, graph in module.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            slot = payload.pop("capability_slot", None)
            if slot is not None:
                capability_id = f"assurance.intake.slot.{slot}"
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


@dataclass(frozen=True)
class ReviewDriveResult:
    public_outcome: str
    successor_count: int
    advance_count: int = 0
    status: str = "completed"
    counters: tuple[int, ...] = ()


class _ScriptedIntakeHost:
    def __init__(self, *, review_decision: str, reviews: tuple[str, ...] = ()) -> None:
        self._reviews = reviews or (review_decision,)
        self._index = 0
        self._advance = None
        self.advance_outputs: list[dict[str, int]] = []

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        if capability_id == _REVIEW_ADVANCE or capability_id.endswith("review-round.advance"):
            if self._advance is None:
                from assurance_intake.operations.workflow_state import ReviewRoundAdvanceHandler

                self._advance = ReviewRoundAdvanceHandler()
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
                self.advance_outputs.append(
                    {
                        "rounds_used": int(cast(int, outcome.output["rounds_used"])),
                        "rounds_budget": int(cast(int, outcome.output["rounds_budget"])),
                    }
                )
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
        decision = self._reviews[min(self._index, len(self._reviews) - 1)]
        if capability_id.endswith("case-review.finalize"):
            self._index += 1
            fixable = decision in {"needs_fix", "changes_requested"}
            human = decision in {"needs_human_review"}
            if decision == "reject":
                fixable = False
                human = False
            output = {
                "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
                "auto_fix_allowed": fixable,
                "auto_fix_plan": [{"fix": "tighten assertion"}] if fixable else [],
                "change_id": change_id,
                "decision": decision,
                "human_review_required": human,
                "public_outcome": {
                    "pass": "pass",
                    "approved": "pass",
                    "needs_fix": "needs_fix",
                    "changes_requested": "needs_fix",
                    "needs_human_review": "needs_human",
                    "reject": "reject",
                }.get(decision, decision),
                "rounds_budget": rounds_budget,
                "rounds_used": rounds_used,
            }
            if fixable:
                output["public_outcome"] = "needs_fix"
            return TaskHostCallResult(operation="execute", outcome=TaskOutcome.succeeded(output))
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.succeeded(
                {
                    "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
                    "change_id": change_id,
                    "decision": "pass",
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


def _compile_intake_workflow():
    module = _load_module()
    handlers: dict[str, TaskHandler] = {}
    graphs = {}
    for graph_id, graph in module.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            slot = payload.pop("capability_slot", None)
            if slot is not None:
                capability_id = f"assurance.intake.slot.{slot}"
                payload["capability"] = capability_id
                handlers[capability_id] = _FakeSlotHandler()
            elif node.capability is not None:
                handlers[node.capability] = _FakeSlotHandler()
            nodes[node_id] = NodeDef.model_validate(payload)
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})
    from graph_engine.graph.schema import WorkflowDef

    return WorkflowDef(
        name="intake-isolation",
        entrypoints={"prepare": "entry", "case": "case"},
        retry=module.retry,
        timeout=module.timeout,
        graphs=graphs,
    ), handlers


def _successor_count(projection: InvocationProjection, node_id: str) -> int:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    tokens = {item.token_id: item for item in projection.offered_tokens}
    successors: set[str] = set()
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        if graph.graph_id.endswith(".entry") or graph.graph_id == "entry":
            if activation.node_id != node_id:
                continue
            for token_id in activation.token_ids:
                continue
        if activation.node_id == node_id:
            for token in projection.offered_tokens:
                if token.source == node_id and token.target is not None:
                    successors.add(token.target)
    del graphs, tokens
    return len(successors)


def _interrupt_successor(projection: InvocationProjection, node_id: str) -> str | None:
    targets = [
        token.target
        for token in projection.offered_tokens
        if token.source == node_id and token.target is not None
    ]
    return targets[0] if targets else None


def _public_outcome_from_run(
    *,
    status: str,
    projection: InvocationProjection,
    resume_action: str | None,
) -> str:
    ends = []
    for activation in projection.activations:
        if activation.node_id in {"done", "rejected", "exhausted"} and activation.status == "completed":
            ends.append(activation.node_id)
    if ends and ends[-1] == "rejected":
        return "rejected"
    if ends and ends[-1] == "exhausted":
        return "exhausted"
    if ends and ends[-1] == "done" and status == "succeeded":
        return "passed"
    if resume_action is not None:
        successor = _interrupt_successor(projection, "human-review")
        if successor == "rejected":
            return "rejected"
        if successor == "done":
            return "passed"
        if successor == "exhausted":
            return "exhausted"
        if successor is not None and successor.startswith("review-round-advance"):
            return "rework"
    if status == "succeeded":
        return "passed"
    return status


def drive_case_review_interrupt(action: str) -> ReviewDriveResult:
    return _drive_review(review_decision="needs_human_review", resumes=(action,))


def _drive_review(
    *,
    review_decision: str,
    reviews: tuple[str, ...] = (),
    resume: str | None = None,
    resumes: tuple[str, ...] = (),
    entrypoint: str = "prepare",
) -> ReviewDriveResult:
    from tests.product.runtime_composition import resolve_workflow_composition

    workflow, handlers = _compile_intake_workflow()
    document = workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)
    composition = resolve_workflow_composition(document, handlers)
    host = _ScriptedIntakeHost(review_decision=review_decision, reviews=reviews)
    actions = resumes if resumes else ((resume,) if resume is not None else ())
    with TemporaryDirectory(prefix="intake-review-drive-") as raw:
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
                entrypoint=entrypoint,
                invocation_id=f"intake-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, dict(_PUBLIC_INPUT))),
                authorization=empty_runtime_authorization(),
                workspace_binding=InvocationWorkspaceBinding(
                    project_root=project,
                    attempts_root=attempts,
                    receipts_root=receipts,
                ),
            )
            result = engine.run_until_blocked(handle)
            for action in actions:
                if result.status != "interrupted":
                    break
                handle = engine.resume(handle, action=action, payload={"decision": action})
                result = engine.run_until_blocked(handle)
            projection = result.projection
            node_id = "human-review"
            return ReviewDriveResult(
                public_outcome=_public_outcome_from_run(
                    status=result.status,
                    projection=projection,
                    resume_action=actions[0] if actions else None,
                ),
                successor_count=_successor_count(projection, node_id if actions else "case-review"),
                advance_count=len(host.advance_outputs),
                status=result.status,
                counters=tuple(item["rounds_used"] for item in host.advance_outputs),
            )
        finally:
            engine.close()


@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", "passed"), ("reject", "rejected"), ("request_rework", "rework")],
)
def test_case_review_action_has_one_successor(action: str, expected: str) -> None:
    result = drive_case_review_interrupt(action)
    assert result.public_outcome == expected
    assert result.successor_count == 1


def test_case_review_pass_completes_without_advance() -> None:
    result = _drive_review(review_decision="pass")
    assert result.public_outcome == "passed"
    assert result.successor_count == 1
    assert result.advance_count == 0


def test_entry_review_routing_is_exclusive() -> None:
    module = _load_module()
    entry = module.graphs["entry"]
    review = entry.nodes["case-review"]
    assert review.routing is not None
    assert review.routing.mode == "exclusive"
    human = entry.nodes["human-review"]
    assert tuple(human.actions) == ("approve", "reject", "request_rework")
    assert human.routing is not None
    assert human.routing.mode == "exclusive"
    assert "review-round-advance" in entry.nodes
    assert entry.nodes["review-round-advance"].capability == _REVIEW_ADVANCE
    assert "rejected" in entry.nodes
    assert "exhausted" in entry.nodes
    assert "review-pass-gate" not in entry.nodes


def test_automatic_fix_advances_once_per_loop() -> None:
    result = _drive_review(review_decision="needs_fix", reviews=("needs_fix", "pass"))
    assert result.public_outcome == "passed"
    assert result.advance_count == 1
    assert result.counters == (1,)


def test_review_budget_exhaustion_is_explicit_non_achieved() -> None:
    result = _drive_review(
        review_decision="needs_fix",
        reviews=("needs_fix", "needs_fix", "needs_fix"),
    )
    assert result.public_outcome == "exhausted"
    assert result.advance_count == 2
    assert result.counters == (1, 2)
    assert result.successor_count == 1


def test_request_rework_advances_once_per_loop() -> None:
    result = _drive_review(
        review_decision="needs_human_review",
        reviews=("needs_human_review", "pass"),
        resumes=("request_rework",),
    )
    assert result.public_outcome == "passed"
    assert result.advance_count == 1
    assert result.counters == (1,)


def test_request_rework_then_auto_fix_uses_advance_counters() -> None:
    result = _drive_review(
        review_decision="needs_human_review",
        reviews=("needs_human_review", "needs_fix", "pass"),
        resumes=("request_rework",),
    )
    assert result.public_outcome == "passed"
    assert result.advance_count == 2
    assert result.counters == (1, 2)


def test_request_rework_exhausts_without_max_activations() -> None:
    result = _drive_review(
        review_decision="needs_human_review",
        reviews=("needs_human_review", "needs_human_review", "needs_human_review"),
        resumes=("request_rework", "request_rework", "request_rework"),
    )
    assert result.public_outcome == "exhausted"
    assert result.advance_count == 2
    assert result.counters == (1, 2)
    assert result.status == "succeeded"
