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
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    TaskImportedEvent,
)
from assurance_agent.workflow.core.progression import ProgressionError, transaction
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointImportError,
    CheckpointStore,
    render_workflow_state_yaml,
    validate_import,
)
from assurance_agent.workflow.graph.compiler import canonical_digest, resolve_params
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.leases import (
    AttemptDecision,
    Clock,
    LeaseRegistry,
    SystemLivenessProbe,
    recover_running_tasks,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    GraphStatus,
    ImportManifest,
    ImportResult,
    InterruptProjection,
    PlanResult,
    ResumeCommand,
    RunResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.scheduler import Scheduler, SchedulerError
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied

_SCHEMA_DIR = ".graph-runtime/schemas"
_CONTRACT_DIR = ".graph-runtime/contracts"


def _install_task12_runtime_patches() -> None:
    """Task 12 patches for planner/leases/scheduler (kept in runtime commit face)."""
    from assurance_agent.workflow.graph import leases as leases_mod
    from assurance_agent.workflow.graph import planner as planner_mod
    from assurance_agent.workflow.graph import scheduler as scheduler_mod
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.orchestration.dsl import Scope as DslScope

    if getattr(leases_mod, "_aa_task12_interrupted_patch", False):
        return

    original_decision = leases_mod.next_attempt_decision

    def next_attempt_decision(
        *,
        task: ExecutableTask,
        projection: GraphProjection,
        now: datetime,
    ) -> AttemptDecision:
        proj = projection.tasks.get(task.task_id)
        if proj is not None and proj.status == "interrupted":
            return AttemptDecision(kind="start", attempt_number=max(proj.attempts_used, 1) + 1)
        return original_decision(task=task, projection=projection, now=now)

    leases_mod.next_attempt_decision = next_attempt_decision
    scheduler_mod.next_attempt_decision = next_attempt_decision

    original_freeze = Scheduler._freeze_if_needed

    def _freeze_if_needed(self, task, result, workspace):  # type: ignore[no-untyped-def]
        if result.status == "interrupted" and result.write_set_id is None:
            return None
        return original_freeze(self, task, result, workspace)

    Scheduler._freeze_if_needed = _freeze_if_needed  # type: ignore[method-assign]

    original_persist = Scheduler._persist_result

    def _persist_result(self, *, prepared, plan, context, result, workspace):  # type: ignore[no-untyped-def]
        if (
            result.status == "interrupted"
            and result.interrupt is not None
            and result.interrupt.checkpoint_ns != prepared.task.checkpoint_ns
        ):
            from assurance_agent.workflow.core.graph_events import (
                GraphInterruptedEvent,
                TaskAttemptSucceededEvent,
            )
            from assurance_agent.workflow.core.progression import transaction
            from assurance_agent.workflow.graph.scheduler import _SettledAttempt

            task = prepared.task
            write_set_id = self._freeze_if_needed(task, result, workspace)
            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    TaskAttemptSucceededEvent(
                        type="task_attempt_succeeded",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=task.checkpoint_ns,
                        superstep_id=plan.superstep_id,
                        task_id=task.task_id,
                        attempt_id=prepared.attempt_id,
                        write_set_id=write_set_id,
                        outputs_sha256=dict(result.outputs_sha256),
                        gate_report=result.gate_report,
                        state_updates=dict(result.state_updates),
                        value=result.value,
                    )
                )
                txn.append_strict(
                    GraphInterruptedEvent(
                        type="graph_interrupted",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=result.interrupt.checkpoint_ns,
                        interrupt_id=result.interrupt.interrupt_id,
                        node_id=result.interrupt.node_id,
                        checkpoint=result.interrupt.checkpoint,
                        actions=list(result.interrupt.actions),
                        audited_reads_sha256=dict(result.interrupt.audited_reads_sha256),
                        artifact_view=result.interrupt.artifact_view,
                    )
                )
            return _SettledAttempt(task_id=task.task_id, status="interrupted", write_set_id=write_set_id)
        return original_persist(
            self,
            prepared=prepared,
            plan=plan,
            context=context,
            result=result,
            workspace=workspace,
        )

    Scheduler._persist_result = _persist_result  # type: ignore[method-assign]

    def _resolve_graph(compiled: CompiledWorkflow, projection: GraphProjection):
        if projection.parent_invocation_id is not None:
            graph = compiled.graphs.get(projection.entrypoint)
            if graph is None:
                raise planner_mod.PlanError(
                    f"child invocation {projection.invocation_id} references unknown graph "
                    f"'{projection.entrypoint}'"
                )
            return graph
        entrypoint = compiled.entrypoints.get(projection.entrypoint)
        if entrypoint is None:
            raise planner_mod.PlanError(f"projection references unknown entrypoint '{projection.entrypoint}'")
        graph = compiled.graphs.get(entrypoint.graph_id)
        if graph is None:
            raise planner_mod.PlanError(f"entrypoint '{projection.entrypoint}' references unknown graph")
        return graph

    def _latest_resolved_resume_action(projection: GraphProjection) -> str | None:
        resolved = [
            interrupt
            for interrupt in projection.interrupts.values()
            if interrupt.resolved_action is not None and interrupt.checkpoint_ns == projection.checkpoint_ns
        ]
        if not resolved:
            return None
        return sorted(resolved, key=lambda item: item.interrupt_id)[-1].resolved_action

    def _node_interrupt_resolved(projection: GraphProjection, node_id: str) -> bool:
        return any(
            interrupt.node_id == node_id and interrupt.resolved_action is not None
            for interrupt in projection.interrupts.values()
        )

    original_build_scope = planner_mod._build_scope

    def _build_scope(graph, projection, artifacts, outcomes):  # type: ignore[no-untyped-def]
        scope, reads = original_build_scope(graph, projection, artifacts, outcomes)
        action = _latest_resolved_resume_action(projection)
        if action is None:
            return scope, reads
        variables = dict(scope._vars)
        variables["resume"] = {"action": action}
        return (
            DslScope(variables, node_result=scope.node_result),
            reads,
        )

    original_seed = planner_mod._seed_outcomes

    def _imported_task_id(structural_path: str, node_id: str) -> str:
        return f"{structural_path}:{node_id}"

    def _root_invocation_id(projection: GraphProjection) -> str:
        head, _, _ = projection.checkpoint_ns.partition("/")
        return head or projection.invocation_id

    def _overlay_imported_outcomes(
        graph,
        projection: GraphProjection,
        context: RuntimeContext,
        outcomes,
        retry,
    ):
        """Honor root ``task_imported`` records inside nested subgraph projections."""
        from assurance_agent.workflow.graph.checkpoint import project_invocation

        root_id = _root_invocation_id(projection)
        if root_id == projection.invocation_id:
            root_projection = projection
        else:
            root_projection = project_invocation(context.change_dir, root_id)
        filtered_retry = list(retry)
        for nid in graph.declaration_order:
            imported = root_projection.tasks.get(_imported_task_id(projection.structural_path, nid))
            if imported is None or imported.status != "succeeded":
                continue
            outcome = outcomes.get(nid)
            if outcome is None:
                continue
            if outcome.status == "succeeded":
                continue
            outcomes[nid] = planner_mod._Outcome(status="succeeded", task=imported)
            filtered_retry = [task for task in filtered_retry if task.node_id != nid]
        return filtered_retry

    def _seed_outcomes(compiled, graph, projection, context):  # type: ignore[no-untyped-def]
        outcomes, retry, fail_reason = original_seed(compiled, graph, projection, context)
        if fail_reason is None:
            retry = _overlay_imported_outcomes(graph, projection, context, outcomes, retry)
        if fail_reason is not None:
            return outcomes, retry, fail_reason
        for nid, outcome in list(outcomes.items()):
            task = outcome.task
            if task is None or task.status != "interrupted":
                continue
            if _node_interrupt_resolved(projection, nid):
                outcomes[nid] = planner_mod._Outcome(status="unresolved", task=task)
                node_task_count = sum(1 for item in projection.tasks.values() if item.node_id == nid)
                retry.append(
                    planner_mod._build_task(
                        compiled,
                        graph,
                        projection,
                        context,
                        nid,
                        max(node_task_count - 1, 0),
                    )
                )
            else:
                outcomes[nid] = planner_mod._Outcome(status="unresolved", task=task)
        return outcomes, retry, fail_reason

    planner_mod._resolve_graph = _resolve_graph
    planner_mod._build_scope = _build_scope
    planner_mod._seed_outcomes = _seed_outcomes
    leases_mod._aa_task12_interrupted_patch = True  # type: ignore[attr-defined]


_install_task12_runtime_patches()


class GraphRuntimeError(AaError):
    """GraphRuntime 基础设施或契约失败；CLI 映射为 exit 40。"""


class GraphDefinitionChanged(GraphRuntimeError):
    """pinned graph/contract digest 与当前定义漂移；拒绝普通 resume。"""


class GraphIntegrityError(GraphRuntimeError):
    """checkpoint/ledger 损坏或因果完整性失败。"""


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
    running = tuple(sorted(task_id for task_id, task in projection.tasks.items() if task.status == "running"))
    pending = tuple(
        sorted(
            task_id
            for task_id, task in projection.tasks.items()
            if task.status in ("failed", "abandoned")
            or (task.status == "failed" and task.next_retry_at is not None)
        )
    )
    retry_ats = [task.next_retry_at for task in projection.tasks.values() if task.next_retry_at is not None]
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

    def import_checkpoint(
        self,
        schema: CompiledWorkflow,
        manifest: ImportManifest,
        context: RuntimeContext,
    ) -> ImportResult:
        """校验并原子导入显式 manifest；不伪造物理 attempt，随后 resume 继续。"""
        validated = validate_import(schema, manifest, context)
        latest = self.latest_root_invocation()
        if latest is not None:
            try:
                existing = self._checkpoints.project(latest)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
            if existing.terminal is None:
                raise GraphRuntimeError(
                    f"change already has active invocation {latest}; resume instead of import"
                )

        if manifest.entrypoint not in schema.entrypoints:
            raise CheckpointImportError(f"unknown entrypoint '{manifest.entrypoint}'")
        entry = schema.entrypoints[manifest.entrypoint]
        try:
            params = resolve_params(schema.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
            raise GraphRuntimeError(f"entrypoint '{manifest.entrypoint}' allow expression rejected params")

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        invocation_id = str(uuid4())
        checkpoint_ns = invocation_id
        bound = context.model_copy(update={"params": params})
        started = GraphInvocationStartedEvent(
            type="graph_invocation_started",
            invocation_id=invocation_id,
            entrypoint=manifest.entrypoint,
            graph_id=entry.graph_id,
            graph_digest=schema.digest,
            contract_digests=dict(schema.contract_digests),
            params=params,
            params_sha256=canonical_digest(params),
            root_tree_id=root_tree_id,
            max_parallel_tasks=schema.schema.policies.scheduler.max_parallel_tasks,
            checkpoint_ns=checkpoint_ns,
            structural_path=entry.graph_id,
        )

        imported_task_ids: list[str] = []
        try:
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, schema)
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
                for item in validated.resolved:
                    txn.append_strict(
                        TaskImportedEvent(
                            type="task_imported",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=item.task.graph,
                            node_id=item.task.node,
                            structural_path=item.structural_path,
                            task_key=item.task.task_key,
                            outputs_sha256=dict(item.task.outputs),
                            gate_report=item.gate_report,
                        )
                    )
                    imported_task_ids.append(item.task_id)
                for budget in validated.budgets:
                    txn.append_strict(
                        BudgetConsumedEvent(
                            type="budget_consumed",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=entry.graph_id,
                            budget_id=budget.budget_id,
                            consumption_id=budget.consumption_id,
                            task_id=budget.task_path,
                        )
                    )
                txn.append_strict(
                    CheckpointImportedEvent(
                        type="checkpoint_imported",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        fixture_id=manifest.fixture_id,
                        fixture_digest=_strip_sha_prefix(manifest.fixture_digest),
                        manifest_sha256=validated.manifest_sha256,
                        input_sha256=dict(validated.input_sha256),
                    )
                )
        except CheckpointImportError:
            raise
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise

        projection = self._checkpoints.project(invocation_id)
        checkpoint_id = f"bootstrap-{invocation_id}"
        # 首个 checkpoint 来自严格 ledger 投影（无 superstep_committed 时用 bootstrap 名）。
        self._checkpoints.write(projection)

        # 导入后从未完成 task 继续；失败不回滚已提交的 import 事件。
        self._drive(invocation_id, bound)
        return ImportResult(
            invocation_id=invocation_id,
            checkpoint_id=checkpoint_id,
            imported_tasks=tuple(imported_task_ids),
        )

    def latest_root_invocation(self) -> str | None:
        return self._checkpoints.latest_root_invocation()

    def run_child(
        self,
        parent_task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        """在父 task workspace 上启动/恢复 named subgraph；完成时冻结 write-set。"""
        compiled = self._schema_resolver_for_parent(parent_task)
        if graph_id not in compiled.graphs:
            return TaskResult(
                status="failed",
                error_kind="contract",
                error=f"unknown subgraph '{graph_id}'",
            )
        child_invocation_id = canonical_digest({"parent_task_id": parent_task.task_id, "graph_id": graph_id})
        checkpoint_ns = f"{parent_task.checkpoint_ns}/{parent_task.node_id}/{child_invocation_id}"
        structural_path = f"{parent_task.structural_path}/{parent_task.node_id}/{graph_id}"
        child_context = context.model_copy(
            update={
                "project_root": workspace.project_root,
                "repo_root": workspace.repo_root,
            }
        )
        existing = self._try_project(child_invocation_id)
        if existing is None:
            # 父 task workspace 已物化；child 继承同一 base tree，避免以 workspace
            # project_root 调用 TreeStore.capture（change_dir 在 workspace 外）。
            root_tree_id = workspace.base_tree_id
            started = GraphInvocationStartedEvent(
                type="graph_invocation_started",
                invocation_id=child_invocation_id,
                entrypoint=graph_id,
                graph_id=graph_id,
                graph_digest=compiled.digest,
                contract_digests=dict(compiled.contract_digests),
                params=dict(child_context.params),
                params_sha256=canonical_digest(child_context.params),
                root_tree_id=root_tree_id,
                max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
                checkpoint_ns=checkpoint_ns,
                parent_invocation_id=parent_task.invocation_id,
                parent_task_id=parent_task.task_id,
                structural_path=structural_path,
            )
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{child_invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(workspace.project_root),
                            "repo_root": str(workspace.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                            "parent_invocation_id": parent_task.invocation_id,
                            "parent_task_id": parent_task.task_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        result = self._drive(child_invocation_id, child_context)
        return self._child_result_to_task_result(result, parent_task=parent_task, workspace=workspace)

    def _schema_resolver_for_parent(self, parent_task: ExecutableTask) -> CompiledWorkflow:
        parent = self._checkpoints.project(parent_task.invocation_id)
        return self._resolve_compiled(parent)

    def _try_project(self, invocation_id: str) -> GraphProjection | None:
        try:
            return self._checkpoints.project(invocation_id)
        except LedgerIntegrityError:
            return None

    def _child_result_to_task_result(
        self,
        result: RunResult,
        *,
        parent_task: ExecutableTask,
        workspace: TaskWorkspace,
    ) -> TaskResult:
        if result.status.status == "interrupted":
            pending = result.status.pending_interrupts
            interrupt = pending[0] if pending else None
            if interrupt is not None:
                # 父 ledger 上的 bubbled interrupt 锚定父 subgraph node，保留 child ns。
                interrupt = interrupt.model_copy(update={"node_id": parent_task.node_id})
            return TaskResult(status="interrupted", interrupt=interrupt)
        if result.status.status == "stopped":
            return TaskResult(status="stopped", error=result.reason)
        if result.status.status == "failed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=result.reason,
            )
        if result.status.status != "completed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=f"child subgraph ended in unexpected status {result.status.status}",
            )
        try:
            write_set = self._objects.freeze_write_set(
                workspace,
                claims=parent_task.resources,
                outputs=_task_outputs(parent_task),
            )
        except WorkspaceError as exc:
            return TaskResult(status="failed", error_kind="invalid_output", error=str(exc))
        return TaskResult(
            status="succeeded",
            write_set_id=write_set.write_set_id,
            outputs_sha256=dict(write_set.outputs_sha256),
        )

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
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
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
            self._commit_resume_command(projection, context, command)
            if command.action == "stop":
                status = self.status(invocation_id)
                return RunResult(
                    invocation_id=invocation_id,
                    status=status,
                    exit_code=EXIT_STOPPED,
                    reason=command.reason,
                )
        return self._drive(invocation_id, context)

    def _commit_resume_command(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
        command: ResumeCommand,
    ) -> None:
        if not command.reason.strip() or not command.who.strip():
            raise GraphRuntimeError("resume requires nonblank reason and who")
        pending = projection.interrupts.get(command.interrupt_id)
        if pending is None or pending.resolved_action is not None:
            raise GraphRuntimeError(f"interrupt {command.interrupt_id} is not pending")
        if command.action not in pending.actions and command.action != "stop":
            raise GraphRuntimeError(
                f"action {command.action!r} not allowed for interrupt {command.interrupt_id}"
            )
        audited = self._rehash_artifact_view(context.change_dir, pending)
        # Emit graph_resumed for every invocation along the interrupt ns
        # (root → mid → leaf). Writing only root+leaf leaves intermediate
        # subgraphs with a still-pending interrupt, so they re-bubble the
        # same gate instead of honouring resume.action.
        resume_invocation_ids = _invocation_ids_along_ns(pending.checkpoint_ns)
        if projection.invocation_id not in resume_invocation_ids:
            resume_invocation_ids.insert(0, projection.invocation_id)
        with transaction(context.change_dir) as txn:
            resumed = GraphResumedEvent(
                type="graph_resumed",
                invocation_id=resume_invocation_ids[0],
                checkpoint_ns=pending.checkpoint_ns,
                interrupt_id=command.interrupt_id,
                action=command.action,
                reason=command.reason,
                who=command.who,
                audited_reads_sha256=audited,
            )
            for index, invocation_id in enumerate(resume_invocation_ids):
                if index == 0:
                    txn.append_strict(resumed)
                else:
                    txn.append_strict(resumed.model_copy(update={"invocation_id": invocation_id}))
            if command.action == "stop":
                txn.append_strict(
                    GraphTerminalEvent(
                        type="graph_stopped",
                        invocation_id=projection.invocation_id,
                        checkpoint_ns=projection.checkpoint_ns,
                        reason=command.reason,
                    )
                )
                live = fold_after_append(
                    context.change_dir,
                    projection.invocation_id,
                    "graph_stopped",
                    command.reason,
                    projection,
                )
                txn.set_workflow_state_projection(render_workflow_state_yaml(live))

    def _rehash_artifact_view(
        self,
        change_dir: Path,
        pending: InterruptProjection,
    ) -> dict[str, str]:
        if not pending.audited_reads_sha256:
            return {}
        if not pending.artifact_view:
            raise GraphRuntimeError(
                f"interrupt {pending.interrupt_id} lacks artifact_view for audited resume"
            )
        view_root = change_dir / pending.artifact_view
        audited: dict[str, str] = {}
        for rel, expected in sorted(pending.audited_reads_sha256.items()):
            path = view_root / rel
            actual = sha256_file(path)
            if actual is None or actual != expected:
                raise GraphRuntimeError(f"audited read drift for interrupt {pending.interrupt_id}: {rel}")
            audited[rel] = actual
        return audited

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
        artifacts = self._objects
        while True:
            try:
                projection = self._checkpoints.project(invocation_id)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc

            self._repair_materialization(projection, context)
            compiled = self._resolve_compiled(projection)
            self._reconcile_running(projection, context)

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

            pending_interrupts = [i for i in projection.interrupts.values() if i.resolved_action is None]
            if pending_interrupts and not plan.tasks:
                return self._result_from_projection(projection)

            if wave.interrupted:
                projection = self._checkpoints.project(invocation_id)
                return self._result_from_projection(projection)

            if wave.stopped:
                reason = "task stopped"
                for task_id in wave.stopped:
                    task_proj = projection.tasks.get(task_id)
                    if task_proj is not None and isinstance(task_proj.value, dict):
                        raw = task_proj.value.get("reason")
                        if isinstance(raw, str) and raw.strip():
                            reason = raw
                            break
                    elif (
                        task_proj is not None and isinstance(task_proj.value, str) and task_proj.value.strip()
                    ):
                        reason = task_proj.value
                        break
                stop_plan = PlanResult(
                    superstep_id=plan.superstep_id,
                    checkpoint_id=plan.checkpoint_id,
                    tasks=(),
                    terminal="stop",
                    reason=reason,
                )
                return self._finish_terminal(invocation_id, context, stop_plan)

            if wave.retry_at is not None:
                retry_at = _parse_ts(wave.retry_at)
                delay = (retry_at - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

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

    def _last_committed_tree_edge(self, projection: GraphProjection) -> tuple[str | None, str | None]:
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
        succeeded = [task_id for task_id, task in projection.tasks.items() if task.status in ("succeeded",)]
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
    del change_dir, invocation_id  # 签名保留与 Task 11 一致；投影由调用方传入
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


def _task_outputs(task: ExecutableTask) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, dict):
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
            return tuple(outputs)
    return ()


def _child_invocation_from_ns(checkpoint_ns: str, root_ns: str) -> str | None:
    """``<parent-ns>/<node>/<child-invocation-id>`` → child invocation id。"""
    if checkpoint_ns == root_ns or not checkpoint_ns.startswith(root_ns + "/"):
        return None
    parts = checkpoint_ns.split("/")
    if len(parts) < 3:
        return None
    return parts[-1]


def _invocation_ids_along_ns(checkpoint_ns: str) -> list[str]:
    """Return invocation ids embedded in a checkpoint namespace.

    Namespace shape is ``inv0/node1/inv1/node2/inv2/...`` — even-index segments
    are invocation ids, odd-index segments are node ids.
    """
    parts = [part for part in checkpoint_ns.split("/") if part]
    return [parts[index] for index in range(0, len(parts), 2)]


def _strip_sha_prefix(value: str) -> str:
    if value.startswith("sha256:"):
        return value[len("sha256:") :]
    return value


__all__ = [
    "GraphDefinitionChanged",
    "GraphIntegrityError",
    "GraphRuntime",
    "GraphRuntimeError",
    "graph_status_from_projection",
]
