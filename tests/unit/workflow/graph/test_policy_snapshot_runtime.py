"""Invocation policy provenance is pinned to the captured workspace tree."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import pytest

from assurance_agent import resources
from assurance_agent.artifacts.policy import PolicyError, load_policy, load_policy_snapshot, policy_digest
from assurance_agent.verification.profile_manifest import (
    assurance_profile_bytes,
    assurance_profile_digest,
    assurance_profile_snapshot_relpath,
)
from assurance_agent.workflow.graph.definition_pinning import (
    bind_root_definitions,
    inherit_child_definitions,
    policy_snapshot_relpath,
    stage_pinned_definitions,
)
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest
from assurance_agent.eval.fixtures import write_fixture_lock
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, parse_execution_contracts

from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.operation import OperationFn, OperationHandler
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.graph.leases import SystemClock
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    ImportManifest,
    ResumeCommand,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

_POLICY_A = """\
version: 1
human_review_risk_levels: [high, critical]
force_continue_allowed: true
plan_checks:
  l1_path: warn
  shared_factory: warn
  assert_ideal: warn
  capability_keys: warn
coverage_floor:
  risk_high: 0.9
  risk_medium: 0.7
fuzz:
  required_when_endpoint_has_auth: true
healing:
  auth_module: require_human
"""

_POLICY_B = """\
version: 1
human_review_risk_levels: [critical]
force_continue_allowed: false
plan_checks:
  l1_path: warn
  shared_factory: warn
  assert_ideal: block
  capability_keys: require_human
coverage_floor:
  risk_high: 0.95
  risk_medium: 0.8
fuzz:
  required_when_endpoint_has_auth: false
healing:
  auth_module: block
"""

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:observe-policy:
    handler: operation
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
"""


class _MutatingAfterCaptureStore(TreeStore):
    def __init__(
        self,
        change_dir: Path,
        *,
        after_capture: Callable[[], None],
        before_recapture: Callable[[], None],
    ) -> None:
        super().__init__(change_dir)
        self._after_capture = after_capture
        self._before_recapture = before_recapture
        self._capture_count = 0

    def capture(self, project_root: Path, *, repo_root: Path | None = None) -> str:
        if self._capture_count:
            self._before_recapture()
        tree_id = super().capture(project_root, repo_root=repo_root)
        if self._capture_count == 0:
            self._after_capture()
        self._capture_count += 1
        return tree_id


def _write_policy(project: Path, text: str) -> None:
    path = project / ".aa" / "policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    write_aa_config(project)
    return project


def _compile(graphs: str) -> tuple[CompiledWorkflow, ExecutionContractCatalog]:
    text = f"""\
schema_version: "2"
name: policy-snapshot
params:
  run_mode: {{type: enum, values: [full], default: full}}
entrypoints:
  full: {{graph: main, allow: "params.run_mode == 'full'"}}
policies:
  retry:
    never: {{max_attempts: 1, retry_on: []}}
  timeout:
    local: {{run_seconds: 60, heartbeat_seconds: 10}}
  scheduler: {{max_parallel_tasks: 1}}
graphs:
{graphs}
gates:
  policy-review-gate:
    reads: []
"""
    contracts = parse_execution_contracts(_CONTRACTS)
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


def _context(project: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"run_mode": "full"},
    )


def _runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: object,
    store: TreeStore,
    observe: OperationFn,
) -> GraphRuntime:
    change = project / "qa" / "changes" / "CH-1"
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    holder: dict[str, GraphRuntime] = {}

    def run_child(
        task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        return holder["runtime"].run_child(task, graph_id, workspace, context)

    operation = OperationHandler({"operation:observe-policy": observe})
    runner = HandlerNodeRunner(
        {
            "operation:observe-policy": operation,
            "builtin:interrupt": InterruptHandler(compiled),
        },
        namespace_handlers={"graph": SubgraphHandler(run_child)},
        compiled=compiled,
        object_store=store,
    )
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=SystemClock(),
        workspace_backend=workspaces,
        node_runner=runner,
        max_parallel_tasks=1,
        contracts=contracts,  # type: ignore[arg-type]
        state_defs={},
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,  # type: ignore[arg-type]
        node_runner=runner,
        scheduler=scheduler,
        schema_resolver=lambda _digest: compiled,
        clock=SystemClock(),
    )
    holder["runtime"] = runtime
    return runtime


def _started_events(project: Path) -> list[dict[str, object]]:
    return [
        event
        for event in read_events_strict(project / "qa" / "changes" / "CH-1")
        if event["type"] == "graph_invocation_started"
    ]


def _seed_fixture(project: Path) -> str:
    fixtures = project / "eval-fixtures"
    sample = fixtures / "samples" / "policy-snapshot"
    sample.mkdir(parents=True)
    (sample / "marker.txt").write_text("fixture\n", encoding="utf-8")
    write_fixture_lock(fixtures, {"policy-snapshot": "samples/policy-snapshot"})
    lock = json.loads((fixtures / "fixture-lock.json").read_text(encoding="utf-8"))
    return str(lock["fixtures"]["policy-snapshot"]["aggregate_sha256"])


def _manifest(fixture_digest: str) -> ImportManifest:
    return ImportManifest(
        schema_version="2",
        entrypoint="full",
        source_kind="eval-fixture",
        fixture_id="policy-snapshot",
        fixture_digest=fixture_digest,
    )


def test_legacy_policy_yaml_digest_includes_defaulted_evidence_sufficiency(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    legacy_digest = policy_digest(load_policy(project))
    _write_policy(
        project,
        _POLICY_A + "evidence_sufficiency:\n"
        "  recency_hours: 48\n"
        "  required_kinds:\n"
        "    API: [covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    assert policy_digest(load_policy(project)) != legacy_digest


def test_root_start_digest_comes_from_the_captured_policy_snapshot(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    observed: list[str] = []

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, context
        observed.append(policy_digest(load_policy(workspace.project_root)))
        _write_policy(project, _POLICY_A)
        return TaskResult(status="succeeded")

    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_policy(project, _POLICY_B),
        before_recapture=lambda: _write_policy(project, _POLICY_A),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)

    result = runtime.run(compiled, "full", _context(project))

    started = _started_events(project)
    assert len(started) == 1
    snapshot = tmp_path / "root-snapshot"
    root_tree_id = started[0]["root_tree_id"]
    assert isinstance(root_tree_id, str)
    store.materialize(root_tree_id, snapshot)
    frozen_digest = policy_digest(load_policy(snapshot))
    assert result.status.status == "completed"
    assert observed == [frozen_digest]
    assert started[0]["policy_digest"] == frozen_digest

    _write_policy(project, _POLICY_B)
    resumed = runtime.resume(result.invocation_id)
    assert resumed.status.status == "completed"
    assert _started_events(project) == started


def test_active_resume_preserves_the_original_frozen_policy_digest(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 5
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
      human:
        uses: builtin:interrupt
        interrupt:
          reason: policy review
          checkpoint: policy-review-gate
          bind: audited_gate_read
          actions: [stop]
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: human}
    routes:
      - from: human
        select: "resume.action"
        cases: {stop: STOP}
        default: STOP
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        _write_policy(project, _POLICY_A)
        return TaskResult(status="succeeded")

    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_policy(project, _POLICY_B),
        before_recapture=lambda: _write_policy(project, _POLICY_A),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)
    interrupted = runtime.run(compiled, "full", _context(project))
    started = _started_events(project)
    assert interrupted.status.status == "interrupted"
    assert len(interrupted.status.pending_interrupts) == 1

    _write_policy(project, _POLICY_B)
    resumed = runtime.resume(
        interrupted.invocation_id,
        ResumeCommand(
            interrupt_id=interrupted.status.pending_interrupts[0].interrupt_id,
            action="stop",
            reason="preserve the pinned policy provenance",
            who="tester",
        ),
    )

    assert resumed.status.status == "stopped"
    assert _started_events(project) == started


def test_import_start_digest_comes_from_the_captured_policy_snapshot(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fixture_digest = _seed_fixture(project)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    observed: list[str] = []

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, context
        observed.append(policy_digest(load_policy(workspace.project_root)))
        _write_policy(project, _POLICY_A)
        return TaskResult(status="succeeded")

    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_policy(project, _POLICY_B),
        before_recapture=lambda: _write_policy(project, _POLICY_A),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)

    result = runtime.import_checkpoint(compiled, _manifest(fixture_digest), _context(project))

    started = _started_events(project)
    assert len(started) == 1
    snapshot = tmp_path / "import-snapshot"
    root_tree_id = started[0]["root_tree_id"]
    assert isinstance(root_tree_id, str)
    store.materialize(root_tree_id, snapshot)
    frozen_digest = policy_digest(load_policy(snapshot))
    assert result.invocation_id == started[0]["invocation_id"]
    assert observed == [frozen_digest]
    assert started[0]["policy_digest"] == frozen_digest


def test_missing_captured_policy_uses_the_packaged_default(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    observed: list[str] = []

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, context
        observed.append(policy_digest(load_policy(workspace.project_root)))
        (project / ".aa" / "policy.yaml").unlink()
        return TaskResult(status="succeeded")

    policy_path = project / ".aa" / "policy.yaml"
    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_policy(project, _POLICY_B),
        before_recapture=lambda: policy_path.unlink(missing_ok=True),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)

    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)
    snapshot = tmp_path / "missing-policy-snapshot"
    root_tree_id = started[0]["root_tree_id"]
    assert isinstance(root_tree_id, str)
    store.materialize(root_tree_id, snapshot)
    frozen_digest = policy_digest(load_policy(snapshot))
    assert observed == [frozen_digest]
    assert started[0]["policy_digest"] == frozen_digest


def test_symlinked_policy_resolves_inside_the_captured_tree(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    target_dir = project / ".aa" / "real-policy-dir"
    target_dir.mkdir()
    target = target_dir / "organization-policy.yaml"
    target.write_text(_POLICY_B, encoding="utf-8")
    (project / ".aa" / "policy-dir").symlink_to(target_dir.name, target_is_directory=True)
    (project / ".aa" / "policy.yaml").symlink_to("policy-dir/organization-policy.yaml")
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    observed: list[str] = []

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, context
        observed.append(policy_digest(load_policy(workspace.project_root)))
        target.write_text(_POLICY_B, encoding="utf-8")
        return TaskResult(status="succeeded")

    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_text(target, _POLICY_A),
        before_recapture=lambda: _write_text(target, _POLICY_B),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)

    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)
    snapshot = tmp_path / "symlink-policy-snapshot"
    root_tree_id = started[0]["root_tree_id"]
    assert isinstance(root_tree_id, str)
    store.materialize(root_tree_id, snapshot)
    frozen_digest = policy_digest(load_policy(snapshot))
    assert observed == [frozen_digest]
    assert started[0]["policy_digest"] == frozen_digest


def test_directory_valued_policy_link_fails_like_the_materialized_workspace(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    policy_dir = project / ".aa" / "policy-dir"
    policy_dir.mkdir()
    (policy_dir / "marker.txt").write_text("directory\n", encoding="utf-8")
    (project / ".aa" / "policy.yaml").symlink_to(policy_dir.name, target_is_directory=True)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)

    with pytest.raises(PolicyError, match="cannot read.*policy.yaml"):
        runtime.run(compiled, "full", _context(project))


def test_policy_link_to_project_root_fails_like_the_materialized_workspace(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / ".aa" / "policy.yaml").symlink_to("..", target_is_directory=True)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)

    with pytest.raises(PolicyError, match="cannot read.*policy.yaml"):
        load_policy(project)
    with pytest.raises(PolicyError, match="cannot read.*policy.yaml"):
        runtime.run(compiled, "full", _context(project))


def test_child_digest_inherits_the_parent_workspace_snapshot(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      child:
        uses: graph:child
        retry: never
        timeout: local
    edges:
      - {from: START, to: child}
      - {from: child, to: END}
  child:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    observed: list[str] = []

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, context
        observed.append(policy_digest(load_policy(workspace.project_root)))
        _write_policy(project, _POLICY_A)
        return TaskResult(status="succeeded")

    store = _MutatingAfterCaptureStore(
        _context(project).change_dir,
        after_capture=lambda: _write_policy(project, _POLICY_B),
        before_recapture=lambda: _write_policy(project, _POLICY_A),
    )
    runtime = _runtime(project, compiled, contracts, store, observe)

    result = runtime.run(compiled, "full", _context(project))

    started = _started_events(project)
    root = next(event for event in started if event.get("parent_invocation_id") is None)
    child = next(event for event in started if event.get("parent_invocation_id") is not None)
    snapshot = tmp_path / "child-snapshot"
    child_tree_id = child["root_tree_id"]
    assert isinstance(child_tree_id, str)
    store.materialize(child_tree_id, snapshot)
    frozen_digest = policy_digest(load_policy(snapshot))
    assert result.status.status == "completed"
    assert child["root_tree_id"] == root["root_tree_id"]
    assert observed == [frozen_digest]
    assert child["policy_digest"] == frozen_digest


def _binding_fields(event: dict[str, object]) -> dict[str, str]:
    return {
        "policy_digest": str(event["policy_digest"]),
        "policy_origin": str(event["policy_origin"]),
        "gate_semantics_digest": str(event["gate_semantics_digest"]),
        "assurance_profile_digest": str(event["assurance_profile_digest"]),
    }


def _pinned_policy_bytes(change_dir: Path, digest: str) -> bytes:
    return (change_dir / policy_snapshot_relpath(digest)).read_bytes()


def test_root_start_pins_project_policy_snapshot_and_records_origin(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)[0]
    change_dir = _context(project).change_dir
    snapshot = tmp_path / "root-snapshot"
    store.materialize(started["root_tree_id"], snapshot)  # type: ignore[arg-type]
    tree_digest = policy_digest(load_policy(snapshot))
    assert started["event_schema_version"] == 4
    assert started["policy_origin"] == "project"
    assert started["policy_digest"] == tree_digest
    assert started["gate_semantics_digest"] == gate_semantics_digest()
    assert started["assurance_profile_digest"] == assurance_profile_digest()
    pinned = _pinned_policy_bytes(change_dir, str(started["policy_digest"]))
    assert policy_digest(load_policy_snapshot(snapshot).policy) == started["policy_digest"]
    assert pinned == load_policy_snapshot(project).canonical_bytes


def test_root_start_without_project_policy_uses_packaged_default_origin(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)[0]
    change_dir = _context(project).change_dir
    snapshot = tmp_path / "default-snapshot"
    store.materialize(started["root_tree_id"], snapshot)  # type: ignore[arg-type]
    assert not (snapshot / ".aa" / "policy.yaml").exists()
    assert started["policy_origin"] == "packaged_default"
    assert started["policy_digest"] == policy_digest(load_policy_snapshot(project).policy)
    assert (
        _pinned_policy_bytes(change_dir, str(started["policy_digest"]))
        == load_policy_snapshot(project).canonical_bytes
    )


def test_equal_project_and_default_snapshots_share_policy_path_but_record_distinct_origins(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, resources.read_text("schemas", "policy-default.yaml"))
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)[0]
    assert started["policy_origin"] == "project"
    digest = str(started["policy_digest"])
    assert (_context(project).change_dir / policy_snapshot_relpath(digest)).exists()


def test_import_start_uses_the_same_root_binding_path(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fixture_digest = _seed_fixture(project)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.import_checkpoint(compiled, _manifest(fixture_digest), _context(project))

    started = _started_events(project)[0]
    change_dir = _context(project).change_dir
    assert started["event_schema_version"] == 4
    assert started["policy_origin"] == "project"
    assert (
        _pinned_policy_bytes(change_dir, str(started["policy_digest"]))
        == load_policy_snapshot(project).canonical_bytes
    )


def test_child_inherits_parent_binding_and_pinned_bytes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      child:
        uses: graph:child
        retry: never
        timeout: local
    edges:
      - {from: START, to: child}
      - {from: child, to: END}
  child:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))

    started = _started_events(project)
    root = next(event for event in started if event.get("parent_invocation_id") is None)
    child = next(event for event in started if event.get("parent_invocation_id") is not None)
    assert _binding_fields(child) == _binding_fields(root)
    change_dir = _context(project).change_dir
    digest = str(root["policy_digest"])
    assert _pinned_policy_bytes(change_dir, digest) == _pinned_policy_bytes(
        change_dir, str(child["policy_digest"])
    )


def test_missing_pinned_policy_snapshot_fails_child_inheritance(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      child:
        uses: graph:child
        retry: never
        timeout: local
    edges:
      - {from: START, to: child}
      - {from: child, to: END}
  child:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))
    change_dir = _context(project).change_dir
    from assurance_agent.workflow.graph.checkpoint import project_invocation

    root_event = next(
        event for event in _started_events(project) if event.get("parent_invocation_id") is None
    )
    parent_projection = project_invocation(change_dir, str(root_event["invocation_id"]))
    pinned = change_dir / policy_snapshot_relpath(parent_projection.policy_digest)
    pinned.unlink()

    with pytest.raises(PolicyError, match="policy snapshot"):
        inherit_child_definitions(parent=parent_projection, change_dir=change_dir)


def test_tampered_pinned_policy_snapshot_fails_child_inheritance(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      child:
        uses: graph:child
        retry: never
        timeout: local
    edges:
      - {from: START, to: child}
      - {from: child, to: END}
  child:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )

    def observe(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    store = TreeStore(_context(project).change_dir)
    runtime = _runtime(project, compiled, contracts, store, observe)
    runtime.run(compiled, "full", _context(project))
    change_dir = _context(project).change_dir
    from assurance_agent.workflow.graph.checkpoint import project_invocation

    root_event = next(
        event for event in _started_events(project) if event.get("parent_invocation_id") is None
    )
    parent_projection = project_invocation(change_dir, str(root_event["invocation_id"]))
    pinned = change_dir / policy_snapshot_relpath(parent_projection.policy_digest)
    pinned.write_bytes(b"tampered\n")

    with pytest.raises(PolicyError, match="policy snapshot"):
        inherit_child_definitions(parent=parent_projection, change_dir=change_dir)


def test_bind_root_definitions_defaults_to_v4_without_profile_bytes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    change_dir = _context(project).change_dir
    store = TreeStore(change_dir)
    root_tree = store.capture(project)
    binding = bind_root_definitions(store=store, root_tree_id=root_tree)
    assert binding.event_schema_version == 4
    assert binding.assurance_profile_bytes is None
    assert binding.assurance_profile_digest == assurance_profile_digest()


def test_v5_root_binding_stages_profile_snapshot(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    compiled, contracts = _compile(
        """\
  main:
    max_supersteps: 4
    nodes:
      observe:
        uses: operation:observe-policy
        retry: never
        timeout: local
    edges:
      - {from: START, to: observe}
      - {from: observe, to: END}
"""
    )
    change_dir = _context(project).change_dir
    store = TreeStore(change_dir)
    root_tree = store.capture(project)
    binding = bind_root_definitions(store=store, root_tree_id=root_tree, event_schema_version=5)
    assert binding.event_schema_version == 5
    assert binding.assurance_profile_bytes == assurance_profile_bytes()
    from assurance_agent.workflow.core.progression import transaction

    with transaction(change_dir) as txn:
        stage_pinned_definitions(txn, compiled, binding, contracts=contracts)
    rel = assurance_profile_snapshot_relpath(binding.assurance_profile_digest)
    assert (change_dir / rel).read_bytes() == binding.assurance_profile_bytes


def test_v5_child_requires_verified_profile_snapshot(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    change_dir = _context(project).change_dir
    store = TreeStore(change_dir)
    root_tree = store.capture(project)
    binding = bind_root_definitions(store=store, root_tree_id=root_tree, event_schema_version=5)
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.graph.models import GraphProjection

    with transaction(change_dir) as txn:
        txn.write_runtime_file_once(policy_snapshot_relpath(binding.policy_digest), binding.policy_bytes)
        assert binding.assurance_profile_bytes is not None
        txn.write_runtime_file_once(
            assurance_profile_snapshot_relpath(binding.assurance_profile_digest),
            binding.assurance_profile_bytes,
        )
    parent = GraphProjection(
        invocation_id="root",
        entrypoint="full",
        checkpoint_ns="root",
        parent_invocation_id=None,
        parent_task_id=None,
        structural_path="main",
        graph_digest="gd",
        event_schema_version=5,
        contract_digests={},
        policy_digest=binding.policy_digest,
        policy_origin=binding.policy_origin,
        gate_semantics_digest=binding.gate_semantics_digest,
        assurance_profile_digest=binding.assurance_profile_digest,
        params={},
        root_tree_id=root_tree,
        current_tree_id=root_tree,
    )
    child = inherit_child_definitions(parent=parent, change_dir=change_dir)
    assert child.event_schema_version == 5
    assert child.assurance_profile_bytes == binding.assurance_profile_bytes

    (change_dir / assurance_profile_snapshot_relpath(binding.assurance_profile_digest)).unlink()
    with pytest.raises(PolicyError, match="assurance profile snapshot"):
        inherit_child_definitions(parent=parent, change_dir=change_dir)


def test_v4_parent_child_inheritance_does_not_fabricate_profile_snapshot(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    _write_policy(project, _POLICY_A)
    change_dir = _context(project).change_dir
    store = TreeStore(change_dir)
    root_tree = store.capture(project)
    binding = bind_root_definitions(store=store, root_tree_id=root_tree)
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.graph.models import GraphProjection

    with transaction(change_dir) as txn:
        txn.write_runtime_file_once(policy_snapshot_relpath(binding.policy_digest), binding.policy_bytes)
    parent = GraphProjection(
        invocation_id="root",
        entrypoint="full",
        checkpoint_ns="root",
        parent_invocation_id=None,
        parent_task_id=None,
        structural_path="main",
        graph_digest="gd",
        event_schema_version=4,
        contract_digests={},
        policy_digest=binding.policy_digest,
        policy_origin=binding.policy_origin,
        gate_semantics_digest=binding.gate_semantics_digest,
        assurance_profile_digest=binding.assurance_profile_digest,
        params={},
        root_tree_id=root_tree,
        current_tree_id=root_tree,
    )
    child = inherit_child_definitions(parent=parent, change_dir=change_dir)
    assert child.event_schema_version == 4
    assert child.assurance_profile_bytes is None
    assert not (change_dir / assurance_profile_snapshot_relpath(child.assurance_profile_digest)).exists()
