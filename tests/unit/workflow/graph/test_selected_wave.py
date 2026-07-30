"""Preview concrete selected waves without ledger append."""

from __future__ import annotations

import json
from pathlib import Path


from assurance_agent.workflow.graph.compiler import canonical_digest, compile_workflow
from assurance_agent.workflow.graph.contracts import ResourcePath, parse_execution_contracts
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    ResolvedArtifact,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import plan_superstep
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.selected_wave import (
    SelectedInvocationWave,
    derive_child_invocation_id,
    preview_selected_wave,
)

_SYNC_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:sync-write:
    handler: operation
    reads: [project:qa/sync/**]
    writes: [project:qa/sync/**]
    synchronized: [project:qa/sync/**]
    exclusive: [project:sync-registry]
    authorization_writes: [project:qa/sync/**]
    retryable_errors: []
  operation:inactive-write:
    handler: operation
    reads: [project:qa/inactive/**]
    writes: [project:qa/inactive/**]
    synchronized: [project:qa/inactive/**]
    exclusive: [project:inactive-registry]
    authorization_writes: [project:qa/inactive/**]
    retryable_errors: []
  operation:plain:
    handler: operation
    side_effect_free: true
    retryable_errors: []
"""

_NESTED_GRAPH = """
  main:
    max_supersteps: 8
    nodes:
      bootstrap:
        uses: graph:child
        retry: never
      direct-sync:
        uses: operation:sync-write
        retry: never
    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: direct-sync}
      - {from: direct-sync, to: END}
  child:
    max_supersteps: 8
    nodes:
      active-sync:
        uses: operation:sync-write
        retry: never
      dormant-sync:
        uses: operation:inactive-write
        retry: never
    edges:
      - {from: START, to: active-sync}
      - {from: START, to: dormant-sync, when: "false"}
      - {from: active-sync, to: END}
      - {from: dormant-sync, to: END}
"""

_LINEAR_GRAPH = """
  main:
    max_supersteps: 8
    nodes:
      prep:
        uses: operation:plain
        retry: never
      sync-leaf:
        uses: operation:sync-write
        retry: never
    edges:
      - {from: START, to: prep}
      - {from: prep, to: sync-leaf}
      - {from: sync-leaf, to: END}
"""


class _FakeArtifacts:
    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        raise KeyError(logical_path)


def _compile(body: str) -> CompiledWorkflow:
    text = (
        'schema_version: "2"\nname: selected-wave\n'
        "entrypoints:\n  full: {graph: main}\n"
        "policies:\n"
        "  retry:\n    never: {max_attempts: 1, retry_on: []}\n"
        "  scheduler: {max_parallel_tasks: 4}\n"
        "graphs:\n" + body
    )
    contracts = parse_execution_contracts(_SYNC_CONTRACTS)
    return compile_workflow(parse_workflow_v2(text), contracts)


def _context(tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "change",
        change_id="change-1",
    )


def _projection(
    compiled: CompiledWorkflow,
    *,
    invocation_id: str = "inv-1",
    entrypoint: str = "full",
    checkpoint_ns: str | None = None,
    structural_path: str = "main",
    parent_invocation_id: str | None = None,
    tasks: list[TaskProjection] | None = None,
    supersteps: int = 0,
) -> GraphProjection:
    return GraphProjection(
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        checkpoint_ns=checkpoint_ns or invocation_id,
        parent_invocation_id=parent_invocation_id,
        structural_path=structural_path,
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params={},
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=supersteps,
        tasks={task.task_id: task for task in tasks or []},
    )


def _task_projection(task: ExecutableTask, status: str, **overrides: object) -> TaskProjection:
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "node_id": task.node_id,
        "status": status,
        "attempts_used": 1,
        "latest_attempt_id": f"{task.task_id}-a1",
    }
    payload.update(overrides)
    return TaskProjection(**payload)  # type: ignore[arg-type]


def _initial_tasks(compiled: CompiledWorkflow, tmp_path: Path, projection: GraphProjection) -> dict[str, ExecutableTask]:
    plan = plan_superstep(compiled, projection, _context(tmp_path), _FakeArtifacts())
    return {task.node_id: task for task in plan.tasks}


def _preview(
    compiled: CompiledWorkflow,
    projection: GraphProjection,
    tmp_path: Path,
    *,
    child_projections: dict[str, GraphProjection] | None = None,
) -> SelectedInvocationWave | None:
    return preview_selected_wave(
        compiled,
        projection,
        _context(tmp_path),
        _FakeArtifacts(),
        max_parallel_tasks=4,
        child_projections=child_projections or {},
    )


def test_direct_synchronized_leaf_preview(tmp_path: Path) -> None:
    compiled = _compile(_LINEAR_GRAPH)
    projection = _projection(compiled)
    tasks = _initial_tasks(compiled, tmp_path, projection)
    prep = _task_projection(tasks["prep"], "succeeded", outputs_committed=True)
    wave = _preview(compiled, _projection(compiled, tasks=[prep]), tmp_path)

    assert wave is not None
    sync_task = wave.selected_tasks[0]
    assert sync_task.node_id == "sync-leaf"
    assert wave.lock_tokens == ("project:sync-registry",)
    assert wave.synchronized_paths == (ResourcePath.parse("project:qa/sync/**"),)
    assert wave.child_waves == ()
    assert wave.identity_digest == canonical_digest(
        {
            "invocation_id": "inv-1",
            "tasks": [
                {
                    "task_id": sync_task.task_id,
                    "target": sync_task.target,
                    "contract_digest": sync_task.contract_digest,
                    "resources": {
                        "reads": ["project:qa/sync/**"],
                        "writes": ["project:qa/sync/**"],
                        "synchronized": ["project:qa/sync/**"],
                        "exclusive": ["project:sync-registry"],
                        "authorization_writes": ["project:qa/sync/**"],
                    },
                }
            ],
            "child_identities": [],
        }
    )


def test_parent_graph_retains_conservative_tokens_without_speculative_paths(tmp_path: Path) -> None:
    compiled = _compile(_NESTED_GRAPH)
    projection = _projection(compiled)
    tasks = _initial_tasks(compiled, tmp_path, projection)
    bootstrap = tasks["bootstrap"]

    wave = _preview(compiled, projection, tmp_path)

    assert wave is not None
    assert [task.node_id for task in wave.selected_tasks] == ["bootstrap"]
    assert wave.lock_tokens == ("project:inactive-registry", "project:sync-registry")
    assert wave.synchronized_paths == ()
    assert wave.child_waves == ()
    assert derive_child_invocation_id(bootstrap, "child") == canonical_digest(
        {"parent_task_id": bootstrap.task_id, "graph_id": "child"}
    )


def test_existing_child_contributes_concrete_paths(tmp_path: Path) -> None:
    compiled = _compile(_NESTED_GRAPH)
    parent_projection = _projection(compiled)
    parent_tasks = _initial_tasks(compiled, tmp_path, parent_projection)
    bootstrap = parent_tasks["bootstrap"]
    child_id = derive_child_invocation_id(bootstrap, "child")

    child_projection = _projection(
        compiled,
        invocation_id=child_id,
        entrypoint="child",
        checkpoint_ns=child_id,
        structural_path=f"{bootstrap.structural_path}/{bootstrap.node_id}/child",
        parent_invocation_id="inv-1",
    )
    child_wave = _preview(compiled, child_projection, tmp_path)

    assert child_wave is not None
    assert [task.node_id for task in child_wave.selected_tasks] == ["active-sync"]
    assert child_wave.synchronized_paths == (ResourcePath.parse("project:qa/sync/**"),)
    assert child_wave.lock_tokens == ("project:sync-registry",)

    parent_wave = _preview(
        compiled,
        parent_projection,
        tmp_path,
        child_projections={child_id: child_projection},
    )

    assert parent_wave is not None
    assert parent_wave.lock_tokens == ("project:inactive-registry", "project:sync-registry")
    assert parent_wave.synchronized_paths == (ResourcePath.parse("project:qa/sync/**"),)
    assert len(parent_wave.child_waves) == 1
    assert parent_wave.child_waves[0].invocation_id == child_id
    assert parent_wave.child_waves[0].identity_digest == child_wave.identity_digest


def test_uncreated_child_yields_no_synchronized_paths(tmp_path: Path) -> None:
    compiled = _compile(_NESTED_GRAPH)
    projection = _projection(compiled)
    wave = _preview(compiled, projection, tmp_path)

    assert wave is not None
    assert wave.synchronized_paths == ()
    assert wave.child_waves == ()


def test_uncommitted_predecessor_blocks_preview(tmp_path: Path) -> None:
    compiled = _compile(_LINEAR_GRAPH)
    projection = _projection(compiled)
    tasks = _initial_tasks(compiled, tmp_path, projection)
    prep = _task_projection(tasks["prep"], "succeeded", outputs_committed=False)
    projection = _projection(compiled, tasks=[prep])

    plan = plan_superstep(compiled, projection, _context(tmp_path), _FakeArtifacts())
    assert [task.node_id for task in plan.tasks] == ["sync-leaf"]

    assert _preview(compiled, projection, tmp_path) is None


def test_identity_digest_changes_when_task_contract_mutates(tmp_path: Path) -> None:
    compiled = _compile(_LINEAR_GRAPH)
    projection = _projection(compiled)
    tasks = _initial_tasks(compiled, tmp_path, projection)
    prep = _task_projection(tasks["prep"], "succeeded", outputs_committed=True)
    wave = _preview(compiled, _projection(compiled, tasks=[prep]), tmp_path)
    assert wave is not None

    sync_task = wave.selected_tasks[0]
    mutated = sync_task.model_copy(
        update={
            "target": "operation:plain",
            "contract_digest": "sha256:" + "b" * 64,
            "resources": compiled.graphs["main"].nodes["prep"].resources,
        }
    )
    mutated_digest = canonical_digest(
        {
            "invocation_id": wave.invocation_id,
            "tasks": [
                {
                    "task_id": mutated.task_id,
                    "target": mutated.target,
                    "contract_digest": mutated.contract_digest,
                    "resources": {
                        "reads": [],
                        "writes": [],
                        "synchronized": [],
                        "exclusive": [],
                        "authorization_writes": [],
                    },
                }
            ],
            "child_identities": [],
        }
    )
    assert wave.identity_digest != mutated_digest


def test_preview_does_not_append_events(tmp_path: Path) -> None:
    compiled = _compile(_LINEAR_GRAPH)
    projection = _projection(compiled)
    before = json.dumps(projection.model_dump(mode="json"), sort_keys=True)
    _preview(compiled, projection, tmp_path)
    after = json.dumps(projection.model_dump(mode="json"), sort_keys=True)
    assert before == after
