"""Subprocess worker for GraphRuntime persistence-boundary fault injection."""

from __future__ import annotations

import os
import signal
import sys
import time
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

_SCHEMAS = {
    "linear": _LINEAR,
    "siblings": _SIBLINGS,
    "budget": _BUDGET,
    "interrupt": _INTERRUPT,
}


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


def _install_hooks(runtime, point: str) -> None:  # noqa: ANN001
    from assurance_agent.workflow.graph import leases as leases_mod

    sched = runtime._scheduler  # noqa: SLF001
    checkpoints = runtime._checkpoints  # noqa: SLF001

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


def _build(project: Path, schema_key: str):
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

    ops["operation:write-marker"] = write_marker
    ops["operation:write-e2e"] = write_e2e
    ops["operation:consume-budget"] = consume_budget
    ops["operation:interrupt-once"] = interrupt_once
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
    schemas = {compiled.digest: compiled}
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=node_runner,
        scheduler=scheduler,
        schema_resolver=lambda digest: schemas[digest],
        clock=clock,
    )
    return runtime, compiled, change


def main() -> int:
    project = Path(os.environ["AA_FAULT_PROJECT"])
    point = os.environ.get("AA_FAULT_POINT", "")
    mode = os.environ.get("AA_FAULT_MODE", "run")
    schema_key = os.environ.get("AA_FAULT_SCHEMA", "linear")
    sync = _sync()
    sync.mkdir(parents=True, exist_ok=True)
    (sync / "READY").write_text(str(os.getpid()), encoding="utf-8")

    runtime, compiled, change = _build(project, schema_key)
    if mode == "run" and point:
        _install_hooks(runtime, point)

    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.models import RuntimeContext

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
            command = ResumeCommand(
                interrupt_id=pending.interrupt_id,
                action=action,
                reason="fault-test resume",
                who="fault-worker",
            )
        result = runtime.resume(invocation_id, command)
    else:
        result = runtime.run(compiled, "full", context)

    (sync / "DONE").write_text(
        f"{result.exit_code}:{result.status.status}:{result.invocation_id}",
        encoding="utf-8",
    )
    return int(result.exit_code)


if __name__ == "__main__":
    sys.exit(main())
