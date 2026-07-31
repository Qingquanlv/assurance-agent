"""Subprocess worker for GraphRuntime persistence-boundary fault injection."""

from __future__ import annotations

import os
import signal
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:write-marker:
    handler: operation
    side_effect_free: false
    writes: ["repo:tests/api/**"]
    authorization_writes: ["repo:tests/api/**"]
    retryable_errors: [timeout, transport, internal]
  operation:write-e2e:
    handler: operation
    side_effect_free: false
    writes: ["repo:tests/e2e/**"]
    authorization_writes: ["repo:tests/e2e/**"]
    retryable_errors: [timeout, transport, internal]
  operation:consume-budget:
    handler: operation
    side_effect_free: true
  operation:interrupt-once:
    handler: operation
    side_effect_free: true
  operation:update-issue:
    handler: operation
    side_effect_free: false
    reads: ["project:qa/issues/**"]
    writes: ["project:qa/issues/**", "change:results/**"]
    authorization_writes: ["project:qa/issues/**", "change:results/**"]
    synchronized: ["project:qa/issues/**"]
    exclusive: ["project:issue-registry"]
"""

_LINEAR = """\
schema_version: "2"
name: fault-linear
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    flaky:
      max_attempts: 3
      retry_on: [timeout, transport, internal]
      backoff: {initial_seconds: 0.01, multiplier: 1.0, max_seconds: 0.01, jitter: false}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 5
    nodes:
      first:
        uses: operation:write-marker
        outputs: ["repo:tests/api/marker.py"]
        retry: flaky
        timeout: local
    edges:
      - {from: START, to: first}
      - {from: first, to: END}
gates: {}
"""

_SIBLINGS = """\
schema_version: "2"
name: fault-siblings
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    flaky:
      max_attempts: 3
      retry_on: [timeout, transport, internal]
      backoff: {initial_seconds: 0.01, multiplier: 1.0, max_seconds: 0.01, jitter: false}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 5
    nodes:
      api:
        uses: operation:write-marker
        outputs: ["repo:tests/api/marker.py"]
        retry: flaky
        timeout: local
      e2e:
        uses: operation:write-e2e
        outputs: ["repo:tests/e2e/marker.py"]
        retry: flaky
        timeout: local
    edges:
      - {from: START, to: api}
      - {from: START, to: e2e}
      - {from: api, to: END}
      - {from: e2e, to: END}
gates: {}
"""

_BUDGET = """\
schema_version: "2"
name: fault-budget
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 4
    budgets:
      loop: {limit: 2}
    nodes:
      consume:
        uses: operation:consume-budget
        retry: never
        timeout: local
        budget: {consume: loop, "on": committed, exhausted_to: END}
    edges:
      - {from: START, to: consume}
      - {from: consume, to: END}
gates: {}
"""

_INTERRUPT = """\
schema_version: "2"
name: fault-interrupt
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 5
    nodes:
      ask:
        uses: operation:interrupt-once
        retry: never
        timeout: local
      after:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: ask}
      - {from: ask, to: after}
      - {from: after, to: END}
gates: {}
"""

_V5_REVISION_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:seed-plan:
    handler: operation
    side_effect_free: false
    writes: ["change:plans/**", "change:review/**"]
    authorization_writes: ["change:plans/**", "change:review/**"]
    retryable_errors: []
  operation:write-review:
    handler: operation
    side_effect_free: false
    writes: ["change:review/**"]
    authorization_writes: ["change:review/**"]
    retryable_errors: []
  operation:write-checks:
    handler: operation
    side_effect_free: false
    writes: ["change:review/**"]
    authorization_writes: ["change:review/**"]
    retryable_errors: []
  builtin:gate:
    handler: builtin
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
"""

_V5_REVISION = """\
schema_version: "2"
name: fault-v5-revision
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  root: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 12
    nodes:
      branch:
        uses: graph:branch
        retry: never
        timeout: local
    edges:
      - {from: START, to: branch}
      - {from: branch, to: END}
  branch:
    max_supersteps: 12
    nodes:
      cycle:
        uses: graph:review-cycle
        retry: never
        timeout: local
    edges:
      - {from: START, to: cycle}
      - {from: cycle, to: END}
  review-cycle:
    max_supersteps: 16
    budgets:
      fix_attempts: {limit: 3}
    nodes:
      seed:
        uses: operation:seed-plan
        outputs: ["change:plans/synth-plan.md"]
        retry: never
        timeout: local
      review:
        uses: operation:write-review
        outputs: ["change:review/synth-plan-review.json"]
        retry: never
        timeout: local
        budget: {consume: fix_attempts, "on": committed, exhausted_to: END}
      mechanical:
        uses: operation:write-checks
        outputs: ["change:review/synth-plan-checks.json"]
        retry: never
        timeout: local
      gate:
        uses: builtin:gate
        with: {gate: synth-plan-gate}
        retry: never
        timeout: local
      human-review:
        uses: builtin:interrupt
        interrupt:
          reason: synth plan needs human revision
          checkpoint: synth-plan-gate
          bind: audited_gate_read
          actions: [fix_and_proceed, accept_risk, stop]
          manual_revision:
            action: fix_and_proceed
            paths: [change:plans/synth-plan.md]
        retry: never
        timeout: local
    edges:
      - {from: START, to: seed}
      - {from: seed, to: review}
      - {from: review, to: mechanical}
      - {from: mechanical, to: gate}
    routes:
      - from: gate
        select: "node('gate').gate.verdict"
        cases:
          needs_human_review: human-review
          pass: END
        default: STOP
      - from: human-review
        select: "resume.action"
        cases:
          fix_and_proceed: review
          accept_risk: END
          stop: STOP
        default: STOP
gates:
  synth-plan-gate:
    reads: [review/synth-plan-review.json, review/synth-plan-checks.json]
    invalid_json: stop
    missing_field_is: stop
    needs_human_review_when: "synth_plan_review.decision == 'needs_human_review'"
    pass_when: "synth_plan_review.decision == 'pass'"
"""

_SYNC = """\
schema_version: "2"
name: fault-sync
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 5
    nodes:
      update:
        uses: operation:update-issue
        outputs:
          - project:qa/issues/ISSUE-1.json
          - change:results/update.json
        retry: never
        timeout: local
    edges:
      - {from: START, to: update}
      - {from: update, to: END}
gates: {}
"""

_SCHEMAS = {
    "linear": _LINEAR,
    "siblings": _SIBLINGS,
    "budget": _BUDGET,
    "interrupt": _INTERRUPT,
    "v5_revision": _V5_REVISION,
    "sync": _SYNC,
}

_REVISION_FIXTURES: dict[str, dict[str, object]] = {}


class NeverCalledInvoker:
    def invoke(self, request):  # noqa: ANN001, ANN201
        raise AssertionError(f"unexpected agent invoke: {request}")


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def _sync() -> Path:
    return Path(os.environ["AA_FAULT_SYNC"])


def _fault_hit(point: str) -> None:
    if os.environ.get("AA_FAULT_POINT") != point:
        return
    sync = _sync()
    sync.mkdir(parents=True, exist_ok=True)
    (sync / "HIT").write_text(point, encoding="utf-8")
    time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGKILL)


@contextmanager
def force_v5_binding():
    """Force fresh root bindings onto event schema version 5 with profile snapshot.

    Restores the original ``bind_root_definitions`` on exit so in-process suites
    do not leak the monkeypatch across tests. Worker subprocesses may keep the
    patch for the process lifetime; the context manager still restores cleanly.
    """
    from assurance_agent.workflow.graph import definition_pinning, runtime as runtime_mod

    original_pinning = definition_pinning.bind_root_definitions
    original_runtime = runtime_mod.bind_root_definitions

    def _bind_v5(*, store, root_tree_id, event_schema_version=4):  # type: ignore[no-untyped-def]
        return original_pinning(store=store, root_tree_id=root_tree_id, event_schema_version=5)

    definition_pinning.bind_root_definitions = _bind_v5  # type: ignore[assignment]
    runtime_mod.bind_root_definitions = _bind_v5  # type: ignore[assignment]
    try:
        yield
    finally:
        definition_pinning.bind_root_definitions = original_pinning
        runtime_mod.bind_root_definitions = original_runtime


def _install_hooks(runtime, point: str, *, scheduler) -> None:  # noqa: ANN001
    from assurance_agent.workflow.core import events as events_mod
    from assurance_agent.workflow.core import progression as prog_mod
    from assurance_agent.workflow.graph import leases as leases_mod
    from assurance_agent.workflow.graph import manual_revision as manual_revision_mod
    from assurance_agent.workflow.graph import runtime as runtime_mod

    sched = scheduler
    checkpoints = runtime._checkpoints  # noqa: SLF001

    if point == "revision_target_objects":
        capture_orig = manual_revision_mod.capture_revision_candidate

        def capture_and_kill(**kwargs):  # type: ignore[no-untyped-def]
            result = capture_orig(**kwargs)
            _fault_hit("revision_target_objects")
            return result

        manual_revision_mod.capture_revision_candidate = capture_and_kill  # type: ignore[assignment]
        runtime_mod.capture_revision_candidate = capture_and_kill  # type: ignore[assignment]

    if point in {
        "manual_plan_revision_append",
        "graph_resumed_ordinal_0",
        "graph_resumed_ordinal_1",
        "graph_resumed_ordinal_2",
    }:
        append_orig = events_mod.append_event_strict

        def append_and_kill(change_dir, event):  # type: ignore[no-untyped-def]
            append_orig(change_dir, event)
            if hasattr(event, "model_dump"):
                payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
            else:
                payload = dict(event)
            etype = payload.get("type")
            if point == "manual_plan_revision_append" and etype == "manual_plan_revision":
                _fault_hit("manual_plan_revision_append")
            if etype == "graph_resumed" and payload.get("revision_transition_id") is not None:
                ordinal = payload.get("revision_ordinal")
                if point == f"graph_resumed_ordinal_{ordinal}":
                    _fault_hit(point)

        events_mod.append_event_strict = append_and_kill  # type: ignore[assignment]
        prog_mod.append_event_strict = append_and_kill  # type: ignore[assignment]

    if point in {"before_attempt_started", "after_attempt_started"}:
        begin_attempt = sched._begin_attempt  # noqa: SLF001

        def begin(task, plan, projection, context, leases):  # type: ignore[no-untyped-def]
            if point == "before_attempt_started":
                _fault_hit("before_attempt_started")
            prepared = begin_attempt(task, plan, projection, context, leases)
            if prepared is not None and not prepared.bypass and point == "after_attempt_started":
                _fault_hit("after_attempt_started")
            return prepared

        sched._begin_attempt = begin  # type: ignore[method-assign]  # noqa: SLF001

    if point == "handler_before_success":
        persist_success_orig = sched._persist_success  # noqa: SLF001

        def persist_success(**kwargs):  # type: ignore[no-untyped-def]
            _fault_hit("handler_before_success")
            return persist_success_orig(**kwargs)

        sched._persist_success = persist_success  # type: ignore[method-assign]  # noqa: SLF001

    if point == "sibling_success_before_commit":
        commit_wave_orig = sched._commit_wave  # noqa: SLF001
        commit_pending_orig = sched.commit_pending_write_sets

        def commit_pending(**kwargs):  # type: ignore[no-untyped-def]
            _fault_hit("sibling_success_before_commit")
            return commit_pending_orig(**kwargs)

        def commit_wave(**kwargs):  # type: ignore[no-untyped-def]
            _fault_hit("sibling_success_before_commit")
            return commit_wave_orig(**kwargs)

        sched.commit_pending_write_sets = commit_pending  # type: ignore[method-assign]
        sched._commit_wave = commit_wave  # type: ignore[method-assign]  # noqa: SLF001

    if point == "budget_success_transaction":
        persist_budget_orig = sched._persist_success  # noqa: SLF001

        def persist_budget(**kwargs):  # type: ignore[no-untyped-def]
            persist_budget_orig(**kwargs)
            if kwargs["prepared"].task.budget is not None:
                _fault_hit("budget_success_transaction")

        sched._persist_success = persist_budget  # type: ignore[method-assign]  # noqa: SLF001

    if point == "interrupt_event":
        persist_result_orig = sched._persist_result  # noqa: SLF001

        def persist_result(**kwargs):  # type: ignore[no-untyped-def]
            settled = persist_result_orig(**kwargs)
            if settled.status == "interrupted":
                _fault_hit("interrupt_event")
            return settled

        sched._persist_result = persist_result  # type: ignore[method-assign]  # noqa: SLF001

    if point == "tree_pointer_superstep":
        from assurance_agent.workflow.core import progression as prog_mod
        import assurance_agent.workflow.graph.scheduler as sched_mod

        commit_tree_orig = prog_mod.commit_tree_pointer

        def wrapped_commit_tree(*args, **kwargs):  # type: ignore[no-untyped-def]
            _fault_hit("tree_pointer_superstep")
            return commit_tree_orig(*args, **kwargs)

        prog_mod.commit_tree_pointer = wrapped_commit_tree  # type: ignore[assignment]
        sched_mod.commit_tree_pointer = wrapped_commit_tree  # type: ignore[assignment]

    if point == "canonical_materialization":
        repair_ordinary_orig = runtime._repair_ordinary_materialization  # noqa: SLF001

        def repair_ordinary(projection, context):  # type: ignore[no-untyped-def]
            _fault_hit("canonical_materialization")
            return repair_ordinary_orig(projection, context)

        runtime._repair_ordinary_materialization = repair_ordinary  # type: ignore[method-assign]  # noqa: SLF001

    if point == "sync_apply_pending":
        store = runtime._objects  # noqa: SLF001

        def crash_before_sync_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
            _fault_hit("sync_apply_pending")
            raise RuntimeError("fault: sync_apply_pending")

        store.apply_write_sets_to_synchronized_paths = crash_before_sync_apply  # type: ignore[method-assign]

    if point == "sync_ack_pending":
        store = runtime._objects  # noqa: SLF001
        original_apply = store.apply_write_sets_to_synchronized_paths

        def apply_then_kill(*args, **kwargs):  # type: ignore[no-untyped-def]
            original_apply(*args, **kwargs)
            _fault_hit("sync_ack_pending")
            raise RuntimeError("fault: sync_ack_pending")

        store.apply_write_sets_to_synchronized_paths = apply_then_kill  # type: ignore[method-assign]

    if point == "checkpoint_snapshot_write":
        write_checkpoint_orig = checkpoints.write

        def write_checkpoint(projection):  # type: ignore[no-untyped-def]
            _fault_hit("checkpoint_snapshot_write")
            return write_checkpoint_orig(projection)

        checkpoints.write = write_checkpoint  # type: ignore[method-assign]

    if point == "heartbeat_replacement":
        upsert_orig = leases_mod.LeaseRegistry.upsert

        def upsert(self, lease):  # type: ignore[no-untyped-def]
            result = upsert_orig(self, lease)
            _fault_hit("heartbeat_replacement")
            return result

        leases_mod.LeaseRegistry.upsert = upsert  # type: ignore[method-assign]


def _build_v5_revision(project: Path):
    import json

    from assurance_agent.workflow.graph.checkpoint import CheckpointStore
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.graph.handlers.operation import (
        OperationHandler,
        default_operations,
    )
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import build_default_node_runner
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
    from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
    from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime

    change = project / "qa" / "changes" / "CH-1"
    contracts = parse_execution_contracts(_V5_REVISION_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_V5_REVISION), contracts)
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    ops = default_operations()

    def seed_plan(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        plans = workspace.change_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        plan_path = plans / "synth-plan.md"
        if not plan_path.exists():
            plan_path.write_text("# original plan\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def write_review(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        plan_path = workspace.change_dir / "plans" / "synth-plan.md"
        plan_text = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
        decision = "pass" if "revised" in plan_text else "needs_human_review"
        review = workspace.change_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "synth-plan-review.json").write_text(
            json.dumps({"decision": decision}),
            encoding="utf-8",
        )
        return TaskResult(status="succeeded")

    def write_checks(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        review = workspace.change_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "synth-plan-checks.json").write_text(
            json.dumps({"status": "ready", "layer": "synth"}),
            encoding="utf-8",
        )
        return TaskResult(status="succeeded")

    ops["operation:seed-plan"] = seed_plan
    ops["operation:write-review"] = write_review
    ops["operation:write-checks"] = write_checks
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    base = build_default_node_runner(
        NeverCalledInvoker(),
        store,
        contracts,
        compiled=compiled,
        run_child=run_child,
    )
    op_handler = OperationHandler(ops)

    class Combined:
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            if task.target.startswith("operation:"):
                return op_handler.execute(task, workspace, context)
            return base.execute(task, workspace, context)

    node_runner = Combined()
    graph_id = compiled.entrypoints["root"].graph_id
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=clock,
        workspace_backend=workspaces,
        node_runner=node_runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,
        state_defs=dict(compiled.schema.graphs[graph_id].state),
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime, compiled, change, scheduler


def _build(project: Path, schema_key: str):
    if schema_key == "v5_revision":
        return _build_v5_revision(project)

    from assurance_agent.workflow.graph.checkpoint import CheckpointStore
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.graph.handlers.operation import (
        OperationHandler,
        default_operations,
    )
    from assurance_agent.workflow.graph.models import (
        ExecutableTask,
        InterruptProjection,
        RuntimeContext,
        TaskResult,
    )
    from assurance_agent.workflow.graph.runtime import GraphRuntime
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
    from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
    from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime

    change = project / "qa" / "changes" / "CH-1"
    contracts = parse_execution_contracts(_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_SCHEMAS[schema_key]), contracts)
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    ops = default_operations()

    def write_marker(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / "tests" / "api" / "marker.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("api-marker\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def write_e2e(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / "tests" / "e2e" / "marker.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("e2e-marker\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def consume_budget(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="succeeded")

    def interrupt_once(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        return TaskResult(
            status="interrupted",
            interrupt=InterruptProjection(
                interrupt_id="int-1",
                checkpoint_ns=task.checkpoint_ns,
                node_id=task.node_id,
                checkpoint="fault.interrupt",
                actions=("stop", "fix_and_proceed"),
                audited_reads_sha256={},
                artifact_view=None,
            ),
        )

    def update_issue(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        del task
        issue = workspace.project_root / "qa" / "issues" / "ISSUE-1.json"
        issue.parent.mkdir(parents=True, exist_ok=True)
        issue.write_text('{"version":2}\n', encoding="utf-8")
        result_path = workspace.change_dir / "results" / "update.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"updated":true}\n', encoding="utf-8")
        # Unrelated live mutation (outside workspace) must not be rolled into sync apply.
        app = context.project_root / "app" / "source.py"
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_text("unrelated live version 2\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:write-marker"] = write_marker
    ops["operation:write-e2e"] = write_e2e
    ops["operation:consume-budget"] = consume_budget
    ops["operation:interrupt-once"] = interrupt_once
    ops["operation:update-issue"] = update_issue
    handler = OperationHandler(ops)
    node_runner = HandlerNodeRunner({target: handler for target in ops})
    graph_id = compiled.entrypoints["full"].graph_id
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=clock,
        workspace_backend=workspaces,
        node_runner=node_runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,
        state_defs=dict(compiled.schema.graphs[graph_id].state),
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    return runtime, compiled, change, scheduler


def prepare_interrupted_v5_graph(tmp_path: Path):
    """Build and run the synthetic root→branch→leaf graph until manual revision interrupt."""
    from assurance_agent.workflow.graph.models import RuntimeContext
    from tests.helpers_aa import write_aa_config

    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True, exist_ok=True)
    write_aa_config(project)
    # bind_root_definitions runs during root start; restore immediately after so
    # later in-process tests do not inherit the v5 force-patch.
    with force_v5_binding():
        runtime, compiled, change, scheduler = _build_v5_revision(project)
        context = RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
            params={"run_mode": "full"},
        )
        result = runtime.run(compiled, "root", context)
    assert result.exit_code == 30, result.reason
    assert result.status.status == "interrupted"
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.revision_view is not None
    root_id = result.invocation_id
    _REVISION_FIXTURES[root_id] = {
        "runtime": runtime,
        "compiled": compiled,
        "context": context,
        "change": change,
        "project": project,
        "interrupt_id": interrupt.interrupt_id,
        "revision_view": interrupt.revision_view,
    }
    return runtime, compiled, context, root_id


def edit_recorded_revision_view(root_id: str, content: bytes) -> None:
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    view_rel = fx["revision_view"]
    assert isinstance(view_rel, str)
    plan = change / view_rel / "plans" / "synth-plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes(content)


def fix_and_proceed_command(root_id: str):
    from assurance_agent.workflow.graph.models import ResumeCommand

    fx = _REVISION_FIXTURES[root_id]
    interrupt_id = fx["interrupt_id"]
    assert isinstance(interrupt_id, str)
    return ResumeCommand(
        interrupt_id=interrupt_id,
        action="fix_and_proceed",
        reason="revise synth plan",
        who="reviewer",
    )


def assert_revision_resume_chain(root_id: str, *, expected_ordinals: tuple[int, ...]) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.checkpoint import project_invocation

    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    events = read_events_strict(change)
    revisions = [e for e in events if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    transition_id = revisions[0]["revision_transition_id"]
    resumes = [
        e
        for e in events
        if e.get("type") == "graph_resumed" and e.get("revision_transition_id") == transition_id
    ]
    ordinals = tuple(int(e["revision_ordinal"]) for e in resumes)  # type: ignore[arg-type]
    assert ordinals == expected_ordinals
    assert resumes[0].get("parent_anchor_ref") is None
    for index in range(1, len(resumes)):
        assert resumes[index].get("parent_anchor_ref") is not None
    leaf_id = revisions[0]["invocation_id"]
    assert resumes[-1]["invocation_id"] == leaf_id
    assert resumes[0]["invocation_id"] == root_id
    assert resumes[0].get("revision_chain_length") == len(expected_ordinals)
    leaf_proj = project_invocation(change, str(leaf_id))
    assert leaf_proj.current_tree_id != revisions[0]["base_tree_id"]
    root_proj = project_invocation(change, root_id)
    assert root_proj.terminal == "completed"


def main() -> int:
    project = Path(os.environ["AA_FAULT_PROJECT"])
    point = os.environ.get("AA_FAULT_POINT", "")
    mode = os.environ.get("AA_FAULT_MODE", "run")
    schema_key = os.environ.get("AA_FAULT_SCHEMA", "linear")
    sync = _sync()
    sync.mkdir(parents=True, exist_ok=True)
    (sync / "READY").write_text(str(os.getpid()), encoding="utf-8")

    binding_cm = force_v5_binding() if schema_key == "v5_revision" else nullcontext()
    with binding_cm:
        runtime, compiled, change, scheduler = _build(project, schema_key)
        if point:
            _install_hooks(runtime, point, scheduler=scheduler)

        from assurance_agent.workflow.graph.checkpoint import project_invocation
        from assurance_agent.workflow.graph.models import RuntimeContext

        entrypoint = "root" if schema_key == "v5_revision" else "full"
        context = RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
            params={"run_mode": "full"},
        )

        if mode == "resume":
            invocation_id = os.environ["AA_FAULT_INVOCATION"]
            from assurance_agent.workflow.graph.models import ResumeCommand

            projection = project_invocation(change, invocation_id)
            pending = next(
                (i for i in projection.interrupts.values() if i.resolved_action is None),
                None,
            )
            command = None
            if pending is not None:
                from typing import Literal, cast

                raw_action = "fix_and_proceed" if "fix_and_proceed" in pending.actions else pending.actions[0]
                action = cast(Literal["fix_and_proceed", "accept_risk", "stop"], raw_action)
                reason = os.environ.get("AA_FAULT_RESUME_REASON", "fault-test resume")
                who = os.environ.get("AA_FAULT_RESUME_WHO", "fault-worker")
                command = ResumeCommand(
                    interrupt_id=pending.interrupt_id,
                    action=action,
                    reason=reason,
                    who=who,
                )
            result = runtime.resume(invocation_id, command)
        else:
            result = runtime.run(compiled, entrypoint, context)

    (sync / "DONE").write_text(
        f"{result.exit_code}:{result.status.status}:{result.invocation_id}",
        encoding="utf-8",
    )
    return int(result.exit_code)


if __name__ == "__main__":
    sys.exit(main())
