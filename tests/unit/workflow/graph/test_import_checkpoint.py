"""显式 validated checkpoint import：manifest 解析、结构闭包与原子 ledger 写入。

覆盖：input-only 导入、gated review 导入、fixture digest/输出 hash/不安全路径/
不可能 structural path/前驱闭包缺失/预算消费缺事件/gate 裁决漂移/
fan-out child 缺 task_key。裸 artifact 存在绝不伪造 completed task。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.eval.fixtures import write_fixture_lock
from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointImportError,
    parse_import_manifest,
)
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.graph.leases import SystemClock
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    ImportManifest,
    ImportedBudget,
    ImportedGate,
    ImportedTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from tests.helpers_aa import write_aa_config

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:review:
    handler: operation
    side_effect_free: false
    writes: ["change:review/**"]
    authorization_writes: ["change:review/**"]
  operation:case-op:
    handler: operation
    side_effect_free: false
    writes: ["change:cases/**"]
    authorization_writes: ["change:cases/**"]
  operation:budget-op:
    handler: operation
    side_effect_free: false
    writes: ["change:heal/**"]
    authorization_writes: ["change:heal/**"]
  operation:retro-write:
    handler: operation
    side_effect_free: false
    writes: ["project:qa/retro/**"]
    authorization_writes: ["project:qa/retro/**"]
  builtin:gate:
    handler: builtin
    side_effect_free: true
"""

_LINEAR = """\
schema_version: "2"
name: import-linear
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    nodes:
      first:
        uses: operation:no-op
        retry: never
        timeout: local
      second:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: first}
      - {from: first, to: second}
      - {from: second, to: END}
gates: {}
"""

_GATED = """\
schema_version: "2"
name: import-gated
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    nodes:
      review:
        uses: operation:review
        outputs: ["change:review/api-plan-review.json"]
        gate: api-plan-review-gate
        retry: never
        timeout: local
      after:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: review}
      - {from: review, to: after}
      - {from: after, to: END}
gates:
  api-plan-review-gate:
    reads: [{path: review/api-plan-review.json, as: review}]
    missing_file_is: stop
    needs_human_review_when: "review.decision == 'needs_human_review'"
    pass_when: "review.decision == 'pass'"
    needs_fix_when: "review.decision == 'needs_fix'"
"""

_NESTED = """\
schema_version: "2"
name: import-nested
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    nodes:
      assurance:
        uses: graph:child
        retry: never
        timeout: local
    edges:
      - {from: START, to: assurance}
      - {from: assurance, to: END}
  child:
    max_supersteps: 8
    nodes:
      leaf:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: leaf}
      - {from: leaf, to: END}
gates: {}
"""

_FANOUT = """\
schema_version: "2"
name: import-fanout
params:
  run_mode: {type: enum, values: [full], default: full}
  modules: {type: list, default: [auth]}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    nodes:
      per-module:
        uses: operation:case-op
        fan_out:
          items: params.modules
          item_as: module
          key: "${module}"
          max_items: 4
          completion: all
        outputs: ["change:cases/${module}/case.yaml"]
        retry: never
        timeout: local
    edges:
      - {from: START, to: per-module}
      - {from: per-module, to: END}
gates: {}
"""

_BUDGET = """\
schema_version: "2"
name: import-budget
params:
  run_mode: {type: enum, values: [full], default: full}
  max_heal: {type: int, default: 3}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    budgets:
      heal: {limit: params.max_heal}
    nodes:
      allocate:
        uses: operation:budget-op
        budget: {consume: heal, "on": committed, exhausted_to: END}
        outputs: ["change:heal/alloc.json"]
        retry: never
        timeout: local
    edges:
      - {from: START, to: allocate}
      - {from: allocate, to: END}
gates: {}
"""

_RETRO = """\
schema_version: "2"
name: import-retro
params:
  run_mode: {type: enum, values: [full, retro], default: full}
  retro_id: {type: str, default: ""}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
  retro:
    graph: main
    with: {run_mode: retro}
    allow: "params.run_mode == 'retro'"
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 4
    nodes:
      write:
        uses: operation:retro-write
        outputs: ["project:qa/retro/${params.retro_id}/marker.json"]
        retry: never
        timeout: local
    edges:
      - {from: START, to: write}
      - {from: write, to: END}
gates: {}
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(project)
    return project


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )


def _compile(text: str):
    contracts = parse_execution_contracts(_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(text), contracts)
    return compiled, contracts


def _ops() -> dict[str, OperationFn]:
    ops = default_operations()

    def review(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "review" / "api-plan-review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
        return TaskResult(status="succeeded")

    def case_op(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="succeeded")

    def budget_op(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "heal" / "alloc.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return TaskResult(status="succeeded")

    def retro_write(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        del task
        path = workspace.project_root / "qa/retro" / str(context.params["retro_id"]) / "marker.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:review"] = review
    ops["operation:case-op"] = case_op
    ops["operation:budget-op"] = budget_op
    ops["operation:retro-write"] = retro_write
    return ops


def _build_runtime(project: Path, compiled, contracts) -> GraphRuntime:
    def build(_store, run_child):  # type: ignore[no-untyped-def]
        handler = OperationHandler(_ops())
        targets: dict[str, object] = {name: handler for name in _ops()}
        subgraph = SubgraphHandler(run_child)
        for graph_id in compiled.graphs:
            targets[f"graph:{graph_id}"] = subgraph
        return HandlerNodeRunner(targets)  # type: ignore[arg-type]

    return assemble_graph_runtime(
        project_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        compiled=compiled,
        contracts=contracts,
        build_node_runner=build,
        clock=SystemClock(),
    )


def _seed_fixture(project: Path, *, fixture_id: str = "eval-sample-001") -> str:
    fixtures = project / "eval-fixtures"
    sample = fixtures / "samples" / fixture_id
    sample.mkdir(parents=True)
    (sample / "marker.txt").write_text("fixture\n", encoding="utf-8")
    write_fixture_lock(fixtures, {fixture_id: f"samples/{fixture_id}"})
    lock = json.loads((fixtures / "fixture-lock.json").read_text(encoding="utf-8"))
    return lock["fixtures"][fixture_id]["aggregate_sha256"]


def _base_manifest(
    *,
    fixture_digest: str,
    completed: tuple[ImportedTask, ...] = (),
    inputs: dict[str, str] | None = None,
    budgets: tuple[ImportedBudget, ...] = (),
    entrypoint: str = "full",
) -> ImportManifest:
    return ImportManifest(
        schema_version="2",
        entrypoint=entrypoint,
        source_kind="eval-fixture",
        fixture_id="eval-sample-001",
        fixture_digest=fixture_digest,
        inputs=inputs or {},
        completed=completed,
        budgets=budgets,
    )


# ---------------------------------------------------------------------------
# Step 1: parser / model shape
# ---------------------------------------------------------------------------


def test_parse_import_manifest_flattens_source_block() -> None:
    raw = {
        "schema_version": "2",
        "entrypoint": "full",
        "source": {
            "kind": "eval-fixture",
            "fixture_id": "eval-sample-001",
            "fixture_digest": "abc",
        },
        "inputs": {"change:plans/api-plan.md": "deadbeef"},
        "completed": [
            {
                "path": "main",
                "graph": "main",
                "node": "first",
                "outputs": {"change:out.json": "cafe"},
            }
        ],
        "budgets": [],
    }
    manifest = parse_import_manifest(raw)
    assert manifest.source_kind == "eval-fixture"
    assert manifest.fixture_id == "eval-sample-001"
    assert manifest.fixture_digest == "abc"
    assert manifest.inputs == {"change:plans/api-plan.md": "deadbeef"}
    assert len(manifest.completed) == 1
    assert manifest.completed[0].path == "main"
    assert manifest.completed[0].node == "first"


def test_parse_import_manifest_rejects_extra_fields() -> None:
    raw = {
        "schema_version": "2",
        "entrypoint": "full",
        "source": {
            "kind": "benchmark-seed",
            "fixture_id": "x",
            "fixture_digest": "y",
        },
        "extra_root": True,
    }
    with pytest.raises(CheckpointImportError, match="extra|unknown|forbid"):
        parse_import_manifest(raw)


def test_parse_import_manifest_rejects_ambiguous_graph_node_only_task() -> None:
    raw = {
        "schema_version": "2",
        "entrypoint": "full",
        "source": {
            "kind": "v1-artifact-import",
            "fixture_id": "x",
            "fixture_digest": "y",
        },
        "completed": [{"graph": "main", "node": "first"}],
    }
    with pytest.raises(CheckpointImportError, match="path|ambiguous"):
        parse_import_manifest(raw)


def test_import_manifest_yaml_roundtrip() -> None:
    text = """\
schema_version: "2"
entrypoint: full
source:
  kind: eval-fixture
  fixture_id: eval-sample-001
  fixture_digest: "sha256:abc"
completed: []
"""
    manifest = parse_import_manifest(yaml.safe_load(text))
    assert manifest.fixture_digest == "sha256:abc"
    assert manifest.completed == ()


# ---------------------------------------------------------------------------
# Step 2: validation + runtime import behaviors
# ---------------------------------------------------------------------------


def test_valid_input_only_import(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    plan = project / "qa" / "changes" / "CH-1" / "plans"
    plan.mkdir(parents=True)
    plan_file = plan / "api-plan.md"
    plan_file.write_text("# plan\n", encoding="utf-8")
    plan_hash = sha256_file(plan_file)
    assert plan_hash is not None

    compiled, contracts = _compile(_LINEAR)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        inputs={"change:plans/api-plan.md": plan_hash},
    )
    result = runtime.import_checkpoint(compiled, manifest, _context(project))
    assert result.invocation_id
    assert result.checkpoint_id
    assert result.imported_tasks == ()

    events = read_events_strict(_context(project).change_dir)
    types = [e["type"] for e in events]
    assert "graph_invocation_started" in types
    assert types.count("checkpoint_imported") == 1
    assert "task_imported" not in types
    # Import itself must not fabricate attempts; resume may start remaining work after.
    import_idx = types.index("checkpoint_imported")
    assert "task_attempt_started" not in types[: import_idx + 1]
    assert "task_attempt_succeeded" not in types[: import_idx + 1]
    imported = next(e for e in events if e["type"] == "checkpoint_imported")
    assert imported["fixture_id"] == "eval-sample-001"
    input_sha = imported["input_sha256"]
    assert isinstance(input_sha, dict)
    assert input_sha["change:plans/api-plan.md"] == plan_hash


def test_retro_import_injects_non_empty_retro_id_before_invocation_is_persisted(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_RETRO)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(fixture_digest=digest, entrypoint="retro")
    context = _context(project).model_copy(update={"params": {"run_mode": "retro"}})

    result = runtime.import_checkpoint(compiled, manifest, context)

    events = read_events_strict(_context(project).change_dir)
    started = next(
        event
        for event in events
        if event["type"] == "graph_invocation_started" and event["invocation_id"] == result.invocation_id
    )
    params = started["params"]
    assert isinstance(params, dict)
    retro_id = params["retro_id"]
    assert isinstance(retro_id, str) and retro_id.startswith("retro-")
    assert (project / "qa/retro" / retro_id / "marker.json").read_text(encoding="utf-8") == "{}\n"
    assert runtime.invocation_terminal(result.invocation_id) is not None


def test_valid_completed_review_gate_import(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    review_dir = project / "qa" / "changes" / "CH-1" / "review"
    review_dir.mkdir(parents=True)
    review_file = review_dir / "api-plan-review.json"
    review_file.write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    out_hash = sha256_file(review_file)
    assert out_hash is not None

    compiled, contracts = _compile(_GATED)
    runtime = _build_runtime(project, compiled, contracts)
    # Re-evaluate gate to get expected reads_sha256 for the manifest.
    from assurance_agent.workflow.orchestration.gates import (
        GateEvaluationContext,
        check_gate_in_view,
    )

    report = check_gate_in_view(
        compiled.schema.gates,
        "api-plan-review-gate",
        GateEvaluationContext(
            project_root=project,
            repo_root=project,
            change_dir=_context(project).change_dir,
            change_id="CH-1",
            params={"run_mode": "full"},
            state_values={},
            node_results={},
        ),
    )
    assert report.verdict.value == "pass"

    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="review",
                outputs={"change:review/api-plan-review.json": out_hash},
                gate=ImportedGate(
                    id="api-plan-review-gate",
                    verdict="pass",
                    reads_sha256=dict(report.reads_sha256),
                ),
            ),
        ),
    )
    result = runtime.import_checkpoint(compiled, manifest, _context(project))
    assert result.imported_tasks == ("main:review",)

    events = read_events_strict(_context(project).change_dir)
    assert any(e["type"] == "task_imported" for e in events)
    imported = next(e for e in events if e["type"] == "task_imported")
    assert imported["node_id"] == "review"
    outputs = imported["outputs_sha256"]
    assert isinstance(outputs, dict)
    assert outputs["change:review/api-plan-review.json"] == out_hash
    gate_report = imported["gate_report"]
    assert isinstance(gate_report, dict)
    assert gate_report["verdict"] == "pass"
    assert gate_report["reads_sha256"] == dict(report.reads_sha256)
    # No physical attempt for the imported review task.
    assert not any(e["type"] == "task_attempt_started" and e.get("node_id") == "review" for e in events)


def test_import_does_not_reuse_change_global_accept_risk_decision(tmp_path: Path) -> None:
    """A fresh imported invocation must not inherit an older unscoped decision."""
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    review_dir = project / "qa" / "changes" / "CH-1" / "review"
    review_dir.mkdir(parents=True)
    review_file = review_dir / "api-plan-review.json"
    review_file.write_text(json.dumps({"decision": "needs_human_review"}), encoding="utf-8")
    out_hash = sha256_file(review_file)
    assert out_hash is not None
    append_event_strict(
        _context(project).change_dir,
        {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": "api-plan-review-gate",
            "action": "accept_risk",
            "reason": "accepted for an earlier invocation",
            "who": "reviewer",
            "review_file": "review/api-plan-review.json",
            "review_sha256": out_hash,
        },
    )

    compiled, contracts = _compile(_GATED)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="review",
                outputs={"change:review/api-plan-review.json": out_hash},
                gate=ImportedGate(
                    id="api-plan-review-gate",
                    verdict="pass",
                    reads_sha256={"review/api-plan-review.json": out_hash},
                ),
            ),
        ),
    )

    with pytest.raises(CheckpointImportError, match="gate verdict mismatch"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_wrong_fixture_digest_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_fixture(project)
    compiled, contracts = _compile(_LINEAR)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(fixture_digest="0" * 64)
    with pytest.raises(CheckpointImportError, match="fixture digest"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_output_hash_mismatch_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    review_dir = project / "qa" / "changes" / "CH-1" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "api-plan-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    compiled, contracts = _compile(_GATED)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="review",
                outputs={"change:review/api-plan-review.json": "0" * 64},
                gate=ImportedGate(id="api-plan-review-gate", verdict="pass", reads_sha256={}),
            ),
        ),
    )
    with pytest.raises(CheckpointImportError, match="output hash|hash mismatch"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_unsafe_path_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_LINEAR)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        inputs={"change:../escape.txt": "0" * 64},
    )
    with pytest.raises(CheckpointImportError, match="unsafe"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_impossible_structural_path_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_NESTED)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main/missing-node/child",
                graph="child",
                node="leaf",
            ),
        ),
    )
    with pytest.raises(CheckpointImportError, match="structural path|impossible|unknown"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_missing_predecessor_closure_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_LINEAR)
    runtime = _build_runtime(project, compiled, contracts)
    # Import second without first — predecessor closure incomplete.
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(ImportedTask(path="main", graph="main", node="second"),),
    )
    with pytest.raises(CheckpointImportError, match="predecessor|closure"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_budget_consumer_without_matching_budget_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    heal = project / "qa" / "changes" / "CH-1" / "heal"
    heal.mkdir(parents=True)
    alloc = heal / "alloc.json"
    alloc.write_text("{}", encoding="utf-8")
    out_hash = sha256_file(alloc)
    assert out_hash is not None

    compiled, contracts = _compile(_BUDGET)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="allocate",
                outputs={"change:heal/alloc.json": out_hash},
            ),
        ),
        budgets=(),  # missing budget_consumed companion
    )
    with pytest.raises(CheckpointImportError, match="budget"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_gate_verdict_hash_mismatch_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    review_dir = project / "qa" / "changes" / "CH-1" / "review"
    review_dir.mkdir(parents=True)
    review_file = review_dir / "api-plan-review.json"
    review_file.write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    out_hash = sha256_file(review_file)
    assert out_hash is not None

    compiled, contracts = _compile(_GATED)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="review",
                outputs={"change:review/api-plan-review.json": out_hash},
                gate=ImportedGate(
                    id="api-plan-review-gate",
                    verdict="needs_fix",  # wrong — file says pass
                    reads_sha256={"review/api-plan-review.json": out_hash},
                ),
            ),
        ),
    )
    with pytest.raises(CheckpointImportError, match="gate|verdict"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_fanout_child_missing_task_key_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_FANOUT)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="per-module",
                # fan-out child requires task_key
            ),
        ),
    )
    with pytest.raises(CheckpointImportError, match="task_key"):
        runtime.import_checkpoint(compiled, manifest, _context(project))


def test_valid_nested_structural_path_import(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    compiled, contracts = _compile(_NESTED)
    runtime = _build_runtime(project, compiled, contracts)
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(ImportedTask(path="main/assurance/child", graph="child", node="leaf"),),
    )
    result = runtime.import_checkpoint(compiled, manifest, _context(project))
    assert result.imported_tasks == ("main/assurance/child:leaf",)
    events = read_events_strict(_context(project).change_dir)
    imported = next(e for e in events if e["type"] == "task_imported")
    assert imported["structural_path"] == "main/assurance/child"
    assert imported["graph_id"] == "child"
    assert imported["node_id"] == "leaf"
    # Import event precedes any physical attempt; imported leaf has attempts_used=0.
    from assurance_agent.workflow.graph.checkpoint import project_invocation

    projection = project_invocation(_context(project).change_dir, result.invocation_id)
    # Root may have run assurance via resume; imported child leaf id stays attempt-free if present.
    leaf = projection.tasks.get("main/assurance/child:leaf")
    if leaf is not None:
        assert leaf.attempts_used == 0
        assert leaf.status == "succeeded"


def test_valid_budget_import_appends_budget_consumed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    digest = _seed_fixture(project)
    heal = project / "qa" / "changes" / "CH-1" / "heal"
    heal.mkdir(parents=True)
    alloc = heal / "alloc.json"
    alloc.write_text("{}", encoding="utf-8")
    out_hash = sha256_file(alloc)
    assert out_hash is not None

    compiled, contracts = _compile(_BUDGET)
    runtime = _build_runtime(project, compiled, contracts)
    task_path = "main:allocate"
    manifest = _base_manifest(
        fixture_digest=digest,
        completed=(
            ImportedTask(
                path="main",
                graph="main",
                node="allocate",
                outputs={"change:heal/alloc.json": out_hash},
            ),
        ),
        budgets=(
            ImportedBudget(
                path="main",
                budget_id="heal",
                consumption_id="cons-1",
                task_path=task_path,
            ),
        ),
    )
    result = runtime.import_checkpoint(compiled, manifest, _context(project))
    assert result.imported_tasks == ("main:allocate",)
    events = read_events_strict(_context(project).change_dir)
    assert any(e["type"] == "budget_consumed" for e in events)
    budget = next(e for e in events if e["type"] == "budget_consumed")
    assert budget["budget_id"] == "heal"
    assert budget["consumption_id"] == "cons-1"
    assert budget["task_id"] == "main:allocate"
