"""Ledger-driven GraphRuntime：Plan → Execute → Update 直到稳定返回边界。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.core.graph_events import (
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
)
from assurance_agent.workflow.core.progression import ProgressionError, transaction
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointStore,
    render_workflow_state_yaml,
)
from assurance_agent.workflow.graph.compiler import canonical_digest, resolve_params
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    SystemLivenessProbe,
    recover_running_tasks,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    GraphProjection,
    GraphStatus,
    PlanResult,
    ResumeCommand,
    RunResult,
    RuntimeContext,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.scheduler import Scheduler, SchedulerError
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied

_SCHEMA_DIR = ".graph-runtime/schemas"
_CONTRACT_DIR = ".graph-runtime/contracts"


class GraphRuntimeError(AaError):
    """GraphRuntime 基础设施或契约失败；CLI 映射为 exit 40。"""


class GraphDefinitionChanged(GraphRuntimeError):
    """pinned graph/contract digest 与当前定义漂移；拒绝普通 resume。"""


class GraphIntegrityError(GraphRuntimeError):
    """checkpoint/ledger 损坏或因果完整性失败。"""


class _EmptyArtifacts:
    """最小 ArtifactReader：缺读一律失败，由 planner 按 MISSING 处理。"""

    def read_json(self, tree_id: str, logical_path: str) -> object:
        raise FileNotFoundError(logical_path)


def graph_status_from_projection(
    projection: GraphProjection,
    *,
    pending_write_sets: tuple[str, ...] = (),
) -> GraphStatus:
    """从 ledger 投影派生公开 GraphStatus（不读 workflow-state.yaml）。"""
    pending_interrupts = tuple(
        interrupt
        for _, interrupt in sorted(projection.interrupts.items())
        if interrupt.resolved_action is None
    )
    running = tuple(
        sorted(task_id for task_id, task in projection.tasks.items() if task.status == "running")
    )
    pending = tuple(
        sorted(
            task_id
            for task_id, task in projection.tasks.items()
            if task.status in ("failed", "abandoned")
            or (task.status == "failed" and task.next_retry_at is not None)
        )
    )
    retry_ats = [
        task.next_retry_at
        for task in projection.tasks.values()
        if task.next_retry_at is not None
    ]
    if projection.terminal == "completed":
        status: Literal["running", "interrupted", "completed", "stopped", "failed"] = "completed"
    elif projection.terminal == "stopped":
        status = "stopped"
    elif projection.terminal == "failed":
        status = "failed"
    elif pending_interrupts:
        status = "interrupted"
    else:
        status = "running"
    return GraphStatus(
        invocation_id=projection.invocation_id,
        entrypoint=projection.entrypoint,
        status=status,
        checkpoint_id=projection.latest_checkpoint_id,
        event_seq=projection.event_seq,
        superstep=projection.supersteps,
        running_tasks=running,
        pending_tasks=pending,
        pending_write_sets=pending_write_sets,
        pending_interrupts=pending_interrupts,
        next_retry_at=min(retry_ats) if retry_ats else None,
        budgets=dict(sorted(projection.budgets.items())),
        terminal_reason=projection.terminal_reason,
    )


class GraphRuntime:
    """对外同步的 Plan → Execute → Update 驱动器；并发仅在 Scheduler 内部。"""

    def __init__(
        self,
        *,
        checkpoint_store: CheckpointStore,
        object_store: TreeStore,
        workspace_backend: WorkspaceBackend,
        contracts: ExecutionContractCatalog,
        node_runner: NodeRunner,
        scheduler: Scheduler,
        schema_resolver: Callable[[str], CompiledWorkflow],
        clock: Clock,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._objects = object_store
        self._workspaces = workspace_backend
        self._contracts = contracts
        self._node_runner = node_runner
        self._scheduler = scheduler
        self._schema_resolver = schema_resolver
        self._clock = clock

    def run(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult:
        return self._start_and_drive(schema, entrypoint, context)

    def resume(
        self,
        invocation_id: str,
        command: ResumeCommand | None = None,
    ) -> RunResult:
        return self._recover_and_drive(invocation_id, command)

    def status(self, invocation_id: str) -> GraphStatus:
        projection = self._checkpoints.project(invocation_id)
        return graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(invocation_id),
        )

    def latest_root_invocation(self) -> str | None:
        return self._checkpoints.latest_root_invocation()

    # ------------------------------------------------------------------ start

    def _start_and_drive(
        self,
        compiled: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult:
        latest = self.latest_root_invocation()
        if latest is not None:
            try:
                existing = self._checkpoints.project(latest)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
            if existing.terminal is None:
                raise GraphRuntimeError(
                    f"change already has active invocation {latest}; resume instead of run"
                )

        if entrypoint not in compiled.entrypoints:
            raise GraphRuntimeError(f"unknown entrypoint '{entrypoint}'")
        entry = compiled.entrypoints[entrypoint]
        try:
            params = resolve_params(compiled.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:  # CompileError
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(
            entry.allow_expr, Scope({"params": params})
        ):
            raise GraphRuntimeError(f"entrypoint '{entrypoint}' allow expression rejected params")

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        invocation_id = str(uuid4())
        checkpoint_ns = invocation_id
        params_sha = canonical_digest(params)
        max_parallel = compiled.schema.policies.scheduler.max_parallel_tasks
        graph_id = entry.graph_id

        started = GraphInvocationStartedEvent(
            type="graph_invocation_started",
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            graph_id=graph_id,
            graph_digest=compiled.digest,
            contract_digests=dict(compiled.contract_digests),
            params=params,
            params_sha256=params_sha,
            root_tree_id=root_tree_id,
            max_parallel_tasks=max_parallel,
            checkpoint_ns=checkpoint_ns,
            structural_path=graph_id,
        )
        bound = context.model_copy(update={"params": params})
        try:
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(context.project_root),
                            "repo_root": str(context.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise
        return self._drive(invocation_id, bound)

    def _stage_pinned_definitions(self, txn: object, compiled: CompiledWorkflow) -> None:
        schema_bytes = (
            json.dumps(
                compiled.schema.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        txn.write_runtime_file(f"{_SCHEMA_DIR}/{compiled.digest}.json", schema_bytes)  # type: ignore[attr-defined]
        for target, digest in sorted(compiled.contract_digests.items()):
            contract = self._contracts.contracts.get(target)
            if contract is None:
                continue
            payload = (
                json.dumps(
                    contract.model_dump(mode="json", by_alias=True, exclude_none=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            txn.write_runtime_file(f"{_CONTRACT_DIR}/{digest}.json", payload)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ resume

    def _recover_and_drive(
        self,
        invocation_id: str,
        command: ResumeCommand | None,
    ) -> RunResult:
        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc
        context = self._context_for(projection)
        if projection.terminal is not None:
            status = graph_status_from_projection(
                projection,
                pending_write_sets=self._pending_write_sets(invocation_id),
            )
            return RunResult(
                invocation_id=invocation_id,
                status=status,
                exit_code=_exit_for_status(status.status),
                reason=projection.terminal_reason or status.status,
            )
        if command is not None:
            pending = projection.interrupts.get(command.interrupt_id)
            if pending is None or pending.resolved_action is not None:
                raise GraphRuntimeError(f"interrupt {command.interrupt_id} is not pending")
            if command.action not in pending.actions and command.action != "stop":
                raise GraphRuntimeError(
                    f"action {command.action!r} not allowed for interrupt {command.interrupt_id}"
                )
            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    GraphResumedEvent(
                        type="graph_resumed",
                        invocation_id=invocation_id,
                        checkpoint_ns=projection.checkpoint_ns,
                        interrupt_id=command.interrupt_id,
                        action=command.action,
                        reason=command.reason,
                        who=command.who,
                        audited_reads_sha256=dict(pending.audited_reads_sha256),
                    )
                )
                if command.action == "stop":
                    txn.append_strict(
                        GraphTerminalEvent(
                            type="graph_stopped",
                            invocation_id=invocation_id,
                            checkpoint_ns=projection.checkpoint_ns,
                            reason=command.reason,
                        )
                    )
                    live = self._checkpoints.project(invocation_id)
                    txn.set_workflow_state_projection(render_workflow_state_yaml(live))
            if command.action == "stop":
                status = self.status(invocation_id)
                return RunResult(
                    invocation_id=invocation_id,
                    status=status,
                    exit_code=EXIT_STOPPED,
                    reason=command.reason,
                )
        return self._drive(invocation_id, context)

    def _context_for(self, projection: GraphProjection) -> RuntimeContext:
        change_dir = self._checkpoints._change_dir  # noqa: SLF001
        meta_path = change_dir / ".graph-runtime" / "invocations" / f"{projection.invocation_id}.json"
        project_root = change_dir.parent.parent.parent
        repo_root = project_root
        change_id = change_dir.name
        parent_session_id: str | None = None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            project_root = Path(meta["project_root"])
            repo_root = Path(meta.get("repo_root", project_root))
            change_id = str(meta.get("change_id", change_id))
            parent_session_id = meta.get("parent_session_id")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return RuntimeContext(
            project_root=project_root,
            repo_root=repo_root,
            change_dir=change_dir,
            change_id=change_id,
            params=dict(projection.params),
            parent_session_id=parent_session_id,
        )

    # ------------------------------------------------------------------ drive

    def _drive(self, invocation_id: str, context: RuntimeContext) -> RunResult:
        artifacts = _EmptyArtifacts()
        while True:
            try:
                projection = self._checkpoints.project(invocation_id)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc

            self._repair_materialization(projection, context)
            compiled = self._resolve_compiled(projection)
            self._reconcile_running(projection, context)

            # 刷新投影（abandon 可能已写入）
            projection = self._checkpoints.project(invocation_id)
            if projection.terminal is not None:
                return self._result_from_projection(projection)

            pending_writes = self._pending_write_sets(invocation_id)
            if pending_writes:
                self._retry_pending_update(projection, context)
                projection = self._checkpoints.project(invocation_id)
                if projection.terminal is not None:
                    return self._result_from_projection(projection)
                pending_writes = self._pending_write_sets(invocation_id)

            wait_until = self._earliest_retry_at(projection)
            if wait_until is not None and wait_until > self._clock.now():
                delay = (wait_until - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            try:
                plan = plan_superstep(compiled, projection, context, artifacts)  # type: ignore[arg-type]
            except PlanError as exc:
                message = str(exc)
                if "graph_definition_changed" in message:
                    raise GraphDefinitionChanged(message) from exc
                raise GraphRuntimeError(message) from exc

            if plan.strict_events:
                with transaction(context.change_dir) as txn:
                    for event in plan.strict_events:
                        txn.append_strict(event)

            if plan.terminal is not None:
                return self._finish_terminal(invocation_id, context, plan)

            if not plan.tasks:
                # 仅持久化了决策/展开事件：继续下一轮 Plan
                if plan.strict_events:
                    continue
                raise GraphRuntimeError("planner returned no tasks and no terminal")

            projection = self._checkpoints.project(invocation_id)
            try:
                wave = self._scheduler.execute(plan, projection, context)
            except (SchedulerError, ProgressionError, WorkspaceError) as exc:
                raise GraphRuntimeError(str(exc)) from exc

            projection = self._checkpoints.project(invocation_id)
            if projection.terminal is not None:
                return self._result_from_projection(projection)

            pending_interrupts = [
                i for i in projection.interrupts.values() if i.resolved_action is None
            ]
            if pending_interrupts and not plan.tasks:
                return self._result_from_projection(projection)

            if wave.interrupted:
                projection = self._checkpoints.project(invocation_id)
                return self._result_from_projection(projection)

            if wave.retry_at is not None:
                retry_at = _parse_ts(wave.retry_at)
                delay = (retry_at - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            # 继续下一 superstep（含 Update 失败后的 pending write 重试）
            continue

    def _finish_terminal(
        self,
        invocation_id: str,
        context: RuntimeContext,
        plan: PlanResult,
    ) -> RunResult:
        assert plan.terminal is not None
        if plan.terminal == "interrupt":
            projection = self._checkpoints.project(invocation_id)
            return self._result_from_projection(projection)

        # 终局前若仍有 pending write-set，先重试 Update，避免无提交完成。
        projection = self._checkpoints.project(invocation_id)
        if self._pending_write_sets(invocation_id):
            self._retry_pending_update(projection, context)
            projection = self._checkpoints.project(invocation_id)
            if self._pending_write_sets(invocation_id):
                raise GraphRuntimeError("cannot terminal while write-sets remain pending")

        event_type: Literal["graph_completed", "graph_stopped", "graph_failed"]
        if plan.terminal == "end":
            event_type = "graph_completed"
        elif plan.terminal == "stop":
            event_type = "graph_stopped"
        else:
            event_type = "graph_failed"
        reason = plan.reason or plan.terminal
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                GraphTerminalEvent(
                    type=event_type,
                    invocation_id=invocation_id,
                    checkpoint_ns=projection.checkpoint_ns,
                    reason=reason,
                )
            )
            live = fold_after_append(context.change_dir, invocation_id, event_type, reason, projection)
            txn.set_workflow_state_projection(render_workflow_state_yaml(live))
        return self._result_from_projection(self._checkpoints.project(invocation_id))

    def _result_from_projection(self, projection: GraphProjection) -> RunResult:
        status = graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(projection.invocation_id),
        )
        return RunResult(
            invocation_id=projection.invocation_id,
            status=status,
            exit_code=_exit_for_status(status.status),
            reason=status.terminal_reason or status.status,
        )

    def _resolve_compiled(self, projection: GraphProjection) -> CompiledWorkflow:
        try:
            compiled = self._schema_resolver(projection.graph_digest)
        except Exception as exc:
            raise GraphDefinitionChanged(
                f"schema_resolver failed for digest {projection.graph_digest}: {exc}"
            ) from exc
        if compiled.digest != projection.graph_digest:
            raise GraphDefinitionChanged(
                f"graph_definition_changed: resolver digest {compiled.digest} != "
                f"pinned {projection.graph_digest}"
            )
        if compiled.contract_digests != projection.contract_digests:
            raise GraphDefinitionChanged("graph_definition_changed: contract digests drifted")
        return compiled

    def _reconcile_running(self, projection: GraphProjection, context: RuntimeContext) -> None:
        leases = LeaseRegistry(context.change_dir)
        recover_running_tasks(
            context.change_dir,
            projection=projection,
            leases=leases.read_all(),
            reconnect_for=lambda _task_id: False,
            probe=SystemLivenessProbe(),
            now=self._clock.now(),
        )

    def _repair_materialization(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> None:
        prev, target = self._last_committed_tree_edge(projection)
        if target is None:
            return
        try:
            current = self._objects.capture(context.project_root, repo_root=context.repo_root)
        except WorkspaceError:
            return
        if current == target:
            return
        base = prev if prev is not None else projection.root_tree_id
        try:
            self._objects.apply_tree(context.project_root, target, base_tree_id=base)
        except WorkspaceError as exc:
            raise GraphRuntimeError(f"failed to repair materialization: {exc}") from exc

    def _last_committed_tree_edge(
        self, projection: GraphProjection
    ) -> tuple[str | None, str | None]:
        cursor = projection.root_tree_id
        last_prev: str | None = None
        last_target: str | None = None
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            target_tree = raw.get("target_tree_id")
            if isinstance(target_tree, str):
                last_prev = cursor
                last_target = target_tree
                cursor = target_tree
        return last_prev, last_target

    def _retry_pending_update(self, projection: GraphProjection, context: RuntimeContext) -> None:
        planned = self._last_uncommitted_plan(projection.invocation_id)
        if planned is None:
            return
        succeeded = [
            task_id
            for task_id, task in projection.tasks.items()
            if task.status in ("succeeded",)
        ]
        if not succeeded:
            return
        plan = PlanResult(
            superstep_id=planned["superstep_id"],
            checkpoint_id=planned["checkpoint_id"],
            tasks=(),
        )
        try:
            self._scheduler._commit_wave(  # noqa: SLF001
                plan=plan,
                projection=projection,
                context=context,
                succeeded_ids=succeeded,
            )
        except (WorkspaceError, ProgressionError, SchedulerError, ValueError):
            return

    def _last_uncommitted_plan(self, invocation_id: str) -> dict[str, str] | None:
        last_plan: dict[str, str] | None = None
        committed: set[str] = set()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
                continue
            if raw.get("type") == "superstep_planned":
                sid = raw.get("superstep_id")
                cid = raw.get("checkpoint_id")
                if isinstance(sid, str) and isinstance(cid, str):
                    last_plan = {"superstep_id": sid, "checkpoint_id": cid}
            elif raw.get("type") == "superstep_committed":
                sid = raw.get("superstep_id")
                if isinstance(sid, str):
                    committed.add(sid)
        if last_plan is None or last_plan["superstep_id"] in committed:
            return None
        return last_plan

    def _pending_write_sets(self, invocation_id: str) -> tuple[str, ...]:
        succeeded: list[str] = []
        committed: set[str] = set()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
                continue
            if raw.get("type") == "task_attempt_succeeded":
                write_set_id = raw.get("write_set_id")
                if isinstance(write_set_id, str):
                    succeeded.append(write_set_id)
            elif raw.get("type") == "superstep_committed":
                raw_ids = raw.get("write_set_ids")
                write_set_ids = raw_ids if isinstance(raw_ids, list) else []
                for item in write_set_ids:
                    if isinstance(item, str):
                        committed.add(item)
        return tuple(ws for ws in succeeded if ws not in committed)

    @staticmethod
    def _earliest_retry_at(projection: GraphProjection) -> datetime | None:
        times = [
            _parse_ts(task.next_retry_at)
            for task in projection.tasks.values()
            if task.next_retry_at is not None and task.status == "failed"
        ]
        return min(times) if times else None


def fold_after_append(
    change_dir: Path,
    invocation_id: str,
    event_type: str,
    reason: str,
    projection: GraphProjection,
) -> GraphProjection:
    """事务内尚未落盘时，构造带 terminal 的投影供 workflow-state staging。"""
    terminal_map = {
        "graph_completed": "completed",
        "graph_stopped": "stopped",
        "graph_failed": "failed",
    }
    return projection.model_copy(
        update={
            "terminal": terminal_map[event_type],
            "terminal_reason": reason,
            "event_seq": projection.event_seq + 1,
        }
    )


def _exit_for_status(
    status: Literal["running", "interrupted", "completed", "stopped", "failed"],
) -> Literal[0, 20, 30, 40]:
    if status == "completed":
        return EXIT_COMPLETED  # type: ignore[return-value]
    if status == "stopped":
        return EXIT_STOPPED  # type: ignore[return-value]
    if status == "interrupted":
        return EXIT_HUMAN_REVIEW  # type: ignore[return-value]
    if status == "failed":
        return EXIT_ERROR  # type: ignore[return-value]
    return EXIT_ERROR  # type: ignore[return-value]


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "GraphDefinitionChanged",
    "GraphIntegrityError",
    "GraphRuntime",
    "GraphRuntimeError",
    "graph_status_from_projection",
]
