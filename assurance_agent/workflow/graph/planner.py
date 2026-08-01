"""纯 Plan 语义：节点激活、route 冻结、join、typed reducer 与终局优先级。

``plan_superstep`` 是 ``(CompiledWorkflow, GraphProjection, RuntimeContext,
ArtifactReader) -> PlanResult`` 的纯函数：不触碰磁盘、不读 wall-clock、不依赖
线程完成顺序。所有 ID（task/superstep/activation）由 invocation、namespace、
graph/node 路径、activation ordinal 与 fan-out key 经 canonical SHA-256 派生；
同一投影必然产出逐字节相同的 PlanResult，resume 因此可以安全重放。

核心语义（对齐设计 §6.5/§8.1/§10/§11.3）：

- 普通 node 在至少一条入边 token 被选择后激活（OR 语义，多条互斥入边不形成
  隐式 AND）；``when`` 只在前驱结果稳定后求值。每个决策恰好产生一个
  ``node_activated`` 或 ``node_skipped`` 事件，放在 ``PlanResult.strict_events``
  中且先于 ``superstep_planned``——runtime 必须先持久化这些事件再分发 task，
  使 resume 永不重新求值已冻结的条件（fold 忽略这两类事件，确定性重放是投影
  no-op；崩溃窗口内的同内容重发不改变任何投影）。
- 被 skip 的普通 node 不遍历其普通出边；join 能观察其声明 source 的 skipped
  状态。同一 superstep 内 sibling 的 state/文件写入互相不可见：条件只读
  ``projection.state_values``（最近一次 ``superstep_committed`` 的值）与冻结的
  task 结局，绝不读 sibling 的 pending ``state_updates``。
- route 只读冻结的 task 结局；select 缺失/MISSING 时选择显式 default，否则
  fail closed 到 STOP。
- join：``all`` 等待每个 source succeeded 或 skipped；``all_active`` 等待全部
  已激活 source 成功、忽略 skipped source（全部 skipped 即空 all_active，是
  runtime 错误而非隐式通过）；``any`` 在首个成功 source 后 ready。
- fan-out：首个选中 superstep 求值 ``items`` 并冻结 ``fan_out_expanded``
  （items、display keys、source hashes、structural child IDs），child 按源列表
  顺序（而非 task ID 哈希序）分发；runtime 必须在任何 child 启动前的严格事务中
  持久化展开事件。展开一经冻结，resume 只重放冻结数据——零 child 进入 ledger
  时先对 source hash fail closed（漂移即 ``fan_out_source_drift``），绝不重算
  不同的 item 列表。``completion: all`` 等待每个冻结 child；``reduce`` 按冻结
  item 序（而非完成先后）合并，任一 child failed/retrying/interrupted 时成功
  child 的写入保持 pending，不发布部分聚合（``fan_out_state_updates``）。
- business budget：消耗点在创建任何新 task 前检查 ledger 投影计数，耗尽即把
  token 改道 ``exhausted_to``（consumer 自身记 ``node_skipped``）。被激活的
  consumer task 携带 ``BudgetConsumption`` 标记，``consumption_id`` 按
  invocation/namespace/budget/task 派生——技术 retry 重计划同一 task，同一
  成功只消耗一个单位；失败/abandon 消耗零个单位。success 与
  ``budget_consumed`` 的原子 staging 由 scheduler 按此标记执行（Task 10）。
- 同一决策点上有多条非互斥 token 同时选中同一普通 node 时 fail closed
  （PlanError，提示改用 builtin:join），绝不重复激活。设计 §6.5 把这一拒绝
  划归 compiler；在 compiler 获得该静态检查之前，planner 以运行时守卫兜底。
- 终局优先级：integrity/runtime FAIL > STOP/REJECT > pending interrupt >
  retry pending > completed；出现高优先级结果时不再 Plan 新 task。

跨代再激活（cyclic SCC）：当已 settled 的 node 收到来自非 START 前驱的新
token（例如 ``fix → review``），清除当前代结局并按新 ordinal 再激活；START
入边只在目标尚无任何 task 时投递，避免与回边 token 叠加以致 fail-closed。
嵌套 subgraph 由 runtime 解析 child graph；planner 接受任意已编译 graph。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.core.graph_events import (
    FanOutExpandedEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepPlannedEvent,
    TaskRecoveryRoutedEvent,
)
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ResourceClaims,
    ResourcePath,
    narrow_claims,
)
from assurance_agent.workflow.graph.models import (
    ArtifactReader,
    BudgetConsumption,
    CompiledGraph,
    CompiledWorkflow,
    ExecutableTask,
    FanOutExpansion,
    GraphProjection,
    PlanResult,
    RecoveryContext,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.node_history import fan_out_aggregate_task_id, node_history_key
from assurance_agent.workflow.graph.schema_v2 import (
    BudgetUseDef,
    FanOutDef,
    NodeDef,
    ReduceDef,
    ResourceDef,
    RetryPolicyDef,
    RouteDef,
    StateDef,
    TimeoutPolicyDef,
)
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    DslError,
    Ident,
    Member,
    Scope,
    evaluate,
    is_satisfied,
    parse_expression,
)


PlanErrorKind = Literal["plan_error", "graph_definition_changed"]


class PlanError(AaError):
    """Plan 阶段 fail closed，并携带供 runtime 判别的稳定错误类型。"""

    def __init__(self, message: str, *, error_kind: PlanErrorKind = "plan_error") -> None:
        super().__init__(message)
        self.error_kind = error_kind


_DEFAULT_RETRY = RetryPolicyDef(max_attempts=1)
_DEFAULT_TIMEOUT = TimeoutPolicyDef(run_seconds=3600.0, heartbeat_seconds=30.0)

_Terminal = Literal["end", "stop", "fail", "interrupt"]
_PROJECTION_TERMINAL: dict[str, _Terminal] = {
    "completed": "end",
    "stopped": "stop",
    "failed": "fail",
}


@dataclass(frozen=True)
class _Outcome:
    """node 当前代的冻结结局；只有 succeeded/skipped 对下游是「已解决」。

    ``reached``（默认 True）区分两类 skipped：structurally unreachable
    （前驱从未 succeeded，节点从未收到 token——``reached=False``）vs 节点确实
    被路由到、只是自身 ``when``/fan-out 条件判定不激活（``reached=True``，
    仍是一次有意义的决策）。``all_active`` join 用它区分「整条上游分支本就
    是死路」与「预期至少一个 source 该跑但没跑」。
    """

    status: Literal["succeeded", "skipped", "unresolved"]
    task: TaskProjection | None = None
    reached: bool = True
    recovery_event: TaskRecoveryRoutedEvent | None = None


class _StopResolution(Exception):
    """route select 缺失/MISSING 且无 default：fail closed 到 STOP 的内部信号。"""


@dataclass(frozen=True)
class _Delivery:
    """一次 token 传递的冻结结果：各 target 的选中描述符与终局标记。"""

    selected: dict[str, list[str]]
    end_reached: bool
    stop_reason: str | None
    fail_reason: str | None
    rerouted: dict[str, str]


@dataclass(frozen=True)
class _RecoveryDelivery:
    """Pinned recovery route plus the failed source used to type fallback input."""

    event: TaskRecoveryRoutedEvent
    source: TaskProjection


# ---------------------------------------------------------------------------
# 公开入口


def plan_superstep(
    compiled: CompiledWorkflow,
    projection: GraphProjection,
    context: RuntimeContext,
    artifacts: ArtifactReader,
) -> PlanResult:
    """从 ledger 投影纯函数式地推导下一个 superstep：ready tasks、事件与终局。"""
    graph = _resolve_graph(compiled, projection)
    superstep_id = _superstep_id(projection)
    checkpoint_id = projection.latest_checkpoint_id or f"bootstrap-{projection.invocation_id}"

    def result(
        tasks: Sequence[ExecutableTask] = (),
        events: Sequence[BaseModel] = (),
        terminal: _Terminal | None = None,
        reason: str | None = None,
    ) -> PlanResult:
        return PlanResult(
            superstep_id=superstep_id,
            checkpoint_id=checkpoint_id,
            tasks=tuple(tasks),
            strict_events=tuple(events),
            terminal=terminal,
            reason=reason,
        )

    if projection.graph_digest != compiled.digest:
        raise PlanError(
            "graph_definition_changed: projection digest "
            f"{projection.graph_digest} != compiled digest {compiled.digest}",
            error_kind="graph_definition_changed",
        )
    if projection.contract_digests != compiled.contract_digests:
        raise PlanError(
            "graph_definition_changed: contract digests drifted from compiled pinning",
            error_kind="graph_definition_changed",
        )
    if projection.supersteps >= graph.max_supersteps:
        return result(
            terminal="fail",
            reason=(
                f"max_supersteps {graph.max_supersteps} exhausted after "
                f"{projection.supersteps} projected supersteps"
            ),
        )
    if projection.terminal is not None:
        return result(
            terminal=_PROJECTION_TERMINAL[projection.terminal],
            reason=projection.terminal_reason,
        )

    outcomes, retry_tasks, fail_reason = _seed_outcomes(compiled, graph, projection, context)
    if fail_reason is not None:
        if fail_reason.startswith("__graph_stop__:"):
            return result(terminal="stop", reason=fail_reason.removeprefix("__graph_stop__:"))
        # integrity/runtime FAIL：最高优先级；不完整的 wave 保持 pending。
        return result(terminal="fail", reason=fail_reason)

    recovery_events, recovery_deliveries = _recovery_metadata(projection, outcomes)

    scope, source_reads = _build_scope(graph, projection, artifacts, outcomes)
    delivery = _deliver_tokens(
        compiled,
        graph,
        projection,
        scope,
        outcomes,
        recovery_deliveries,
    )

    # 终局优先级：FAIL > STOP > pending interrupt > retry pending > completed。
    # 高优先级结果一旦出现即不再决策/Plan 新 task（activation 决策是纯函数，
    # resume 可安全重放，因此这里无需带出本批事件）。
    if delivery.fail_reason is not None:
        return result(terminal="fail", reason=delivery.fail_reason)
    if delivery.stop_reason is not None:
        return result(terminal="stop", reason=delivery.stop_reason)
    pending_interrupt = next(
        (i for i in sorted(projection.interrupts) if projection.interrupts[i].resolved_action is None),
        None,
    )
    if pending_interrupt is not None:
        return result(terminal="interrupt", reason=f"interrupt {pending_interrupt} is pending")

    events: list[BaseModel] = list(recovery_events)
    ready: list[ExecutableTask] = []
    _decide_nodes(
        compiled,
        graph,
        projection,
        context,
        scope,
        source_reads,
        delivery,
        outcomes,
        recovery_deliveries,
        events,
        ready,
    )

    ready.extend(retry_tasks)
    ready = [_attach_recovery_context(task, recovery_deliveries) for task in ready]
    child_order = _fan_out_child_order(projection, events)
    ready.sort(key=lambda task: _task_sort_key(graph, task, child_order))
    if ready:
        planned = SuperstepPlannedEvent(
            type="superstep_planned",
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            superstep_id=superstep_id,
            checkpoint_id=checkpoint_id,
            task_ids=[task.task_id for task in ready],
        )
        return result(tasks=ready, events=[*events, planned])

    if any(isinstance(event, FanOutExpandedEvent) for event in events):
        # 零 item 的展开没有 child 可分发；展开事件持久化后，下一代由冻结数据解决。
        return result(events=events)
    if _has_inflight(outcomes):
        # wave 仍在飞行：等待 sibling settle，不 Plan 新 task；本批决策事件仍需持久化。
        return result(events=events)
    if delivery.end_reached:
        return result(events=events, terminal="end", reason="END reached")
    if all(outcomes[nid].status != "unresolved" for nid in graph.nodes):
        return result(
            events=events,
            terminal="fail",
            reason="graph settled without reaching END/STOP/FAIL",
        )
    return result(
        events=events,
        terminal="fail",
        reason="graph stalled: undecided nodes remain with no task in flight",
    )


def apply_state_updates(
    state_defs: Mapping[str, StateDef],
    current: Mapping[str, object],
    updates: Sequence[tuple[str, Mapping[str, object]]],
) -> dict[str, object]:
    """按 structural task ID 排序确定性合并一个 superstep 的 state updates。

    合并顺序只看 task ID，不看完成先后；``replace`` 在同一 superstep 只允许
    一个 writer。输入必须是 declared state key 上的 JSON 可序列化值。
    """
    result = dict(current)
    replace_writers: dict[str, str] = {}
    for task_id, task_updates in sorted(updates, key=lambda pair: pair[0]):
        for key, value in task_updates.items():
            definition = state_defs.get(key)
            if definition is None:
                raise PlanError(f"task {task_id} writes undeclared state key '{key}'")
            if definition.reducer == "replace":
                previous = replace_writers.get(key)
                if previous is not None:
                    raise PlanError(
                        f"state key '{key}' reducer 'replace' has multiple writers "
                        f"in one superstep: {previous}, {task_id}"
                    )
                replace_writers[key] = task_id
            result[key] = _reduce(definition.reducer, result.get(key, definition.default), value)
    return result


# ---------------------------------------------------------------------------
# reducer


def _canonical_json(value: object, *, what: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{what} must be JSON-serializable: {exc}") from exc


def _reduce(reducer: str, current: object, value: object) -> object:
    if reducer == "replace":
        return value
    if reducer == "append":
        if not isinstance(current, list) or not isinstance(value, list):
            raise PlanError("reducer 'append' requires list current value and list update")
        return [*current, *value]
    if reducer == "merge_disjoint":
        if not isinstance(current, dict) or not isinstance(value, dict):
            raise PlanError("reducer 'merge_disjoint' requires object current value and object update")
        duplicates = sorted(str(key) for key in current if key in value)
        if duplicates:
            raise PlanError(
                "reducer 'merge_disjoint' rejects duplicate object keys: " + ", ".join(duplicates)
            )
        return {**current, **value}
    if reducer == "set_union":
        if not isinstance(current, list) or not isinstance(value, list):
            raise PlanError("reducer 'set_union' requires list current value and list update")
        merged: list[object] = []
        seen: set[str] = set()
        for item in (*current, *value):
            marker = _canonical_json(item, what="set_union element")
            if marker not in seen:
                seen.add(marker)
                merged.append(item)
        return sorted(merged, key=lambda item: _canonical_json(item, what="set_union element"))
    raise PlanError(f"unknown reducer '{reducer}'")


# ---------------------------------------------------------------------------
# graph 解析与 scope 构建


def _resolve_graph(compiled: CompiledWorkflow, projection: GraphProjection) -> CompiledGraph:
    if projection.parent_invocation_id is not None:
        graph = compiled.graphs.get(projection.entrypoint)
        if graph is None:
            raise PlanError(
                f"child invocation {projection.invocation_id} references unknown graph "
                f"'{projection.entrypoint}'"
            )
        return graph
    entrypoint = compiled.entrypoints.get(projection.entrypoint)
    if entrypoint is None:
        raise PlanError(f"projection references unknown entrypoint '{projection.entrypoint}'")
    graph = compiled.graphs.get(entrypoint.graph_id)
    if graph is None:
        raise PlanError(f"entrypoint '{projection.entrypoint}' references unknown graph")
    return graph


def _build_scope(
    graph: CompiledGraph,
    projection: GraphProjection,
    artifacts: ArtifactReader,
    outcomes: dict[str, _Outcome],
) -> tuple[Scope, dict[str, str]]:
    """构建 DSL scope：params/state + 冻结 node 结局 + 仅当前 tree 的 artifact symbol。

    只加载 ``CompiledGraph.artifact_symbols`` 声明的 symbol，且只从
    ``projection.current_tree_id`` 读取；JSON 解析失败或 hash 漂移时该 symbol
    按 MISSING 处理（三值 DSL 的 fail-closed 语义，不绑定变量），已成功读取的
    ``reads_sha256`` 并集由调用方带进 activation 事件。``node('id')`` 只读冻结
    的 task 结局与本次决策已冻结的 skip 状态（resolver 闭包随决策推进更新）。
    """
    variables: dict[str, object] = {
        "params": dict(projection.params),
        "state": dict(projection.state_values),
    }
    resume_action = _latest_resolved_resume_action(projection)
    if resume_action is not None:
        variables["resume"] = {"action": resume_action}
    reads: dict[str, str] = {}
    for symbol in sorted(graph.artifact_symbols):
        logical_path = graph.artifact_symbols[symbol]
        try:
            resolved = artifacts.read_json(projection.current_tree_id, logical_path)
        except Exception:  # reader 对缺读/损坏/hash 漂移抛出异常：fail closed 到 MISSING
            continue
        variables[symbol] = resolved.value
        reads.update(resolved.reads_sha256)

    def node_result(node_id: str) -> object:
        outcome = outcomes.get(node_id)
        if outcome is None:
            return {}
        if outcome.status == "skipped":
            return {"status": "skipped"}
        task = outcome.task
        if task is None:
            # fan-out node 全部由冻结 child 组成：解决后只暴露状态，不代理某个 child。
            return {"status": "succeeded"} if outcome.status == "succeeded" else {}
        payload: dict[str, object] = {"status": task.status}
        if task.value is not None:
            payload["value"] = task.value
        if task.gate_report is not None:
            payload["gate"] = task.gate_report
            if "value" in task.gate_report:
                payload["value"] = task.gate_report["value"]
        return payload

    return Scope(variables, node_result=node_result), reads


# ---------------------------------------------------------------------------
# outcome seeding 与 token 传递


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


def _imported_task_id(structural_path: str, node_id: str) -> str:
    return f"{structural_path}:{node_id}"


def _root_invocation_id(projection: GraphProjection) -> str:
    head, _, _ = projection.checkpoint_ns.partition("/")
    return head or projection.invocation_id


def _overlay_imported_outcomes(
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    outcomes: dict[str, _Outcome],
    retry: list[ExecutableTask],
) -> list[ExecutableTask]:
    """Honor root ``task_imported`` records inside nested subgraph projections."""
    from assurance_agent.workflow.graph.checkpoint import project_invocation

    root_id = _root_invocation_id(projection)
    root_projection = (
        projection if root_id == projection.invocation_id else project_invocation(context.change_dir, root_id)
    )
    imported_nodes: set[str] = set()
    for nid in graph.declaration_order:
        imported = root_projection.tasks.get(_imported_task_id(projection.structural_path, nid))
        if imported is None or imported.status != "succeeded":
            continue
        outcome = outcomes.get(nid)
        if outcome is None or outcome.status == "succeeded":
            continue
        outcomes[nid] = _Outcome(status="succeeded", task=imported)
        imported_nodes.add(nid)
    return [task for task in retry if task.node_id not in imported_nodes]


def _seed_outcomes(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
) -> tuple[
    dict[str, _Outcome],
    list[ExecutableTask],
    str | None,
]:
    """把投影中的 task 结局折叠成各 node 当前代 outcome；同时收集 retry 重计划。

    保持 runtime overlay 所依赖的 ``(outcomes, retry_tasks, fail_reason)`` 边界；
    recovery event 暂存于源 outcome，overlay 后再提取为 durable event/delivery。
    retry 耗尽或 error kind 本就不可重试，且命中 recover allowlist 时才建立
    recovery；源 task 始终保持 failed 且不遍历普通出边。
    """
    tasks_by_node: dict[str, list[TaskProjection]] = {}
    for task in projection.tasks.values():
        tasks_by_node.setdefault(task.node_id, []).append(task)

    outcomes: dict[str, _Outcome] = {}
    retry: list[ExecutableTask] = []
    recovery_vias: dict[str, str] = {}
    for nid in graph.declaration_order:
        node_tasks = tasks_by_node.get(nid, [])
        definition = graph.nodes[nid].definition
        if definition.fan_out is not None:
            outcome, child_retry, child_fail = _seed_fan_out(
                compiled, graph, projection, context, nid, node_tasks
            )
            if child_fail is not None:
                return outcomes, retry, child_fail
            outcomes[nid] = outcome
            retry.extend(child_retry)
            continue
        if not node_tasks:
            outcomes[nid] = _Outcome(status="unresolved")
            continue
        latest = _latest_task(graph, projection, nid, node_tasks)
        if latest.status == "succeeded" and _task_ready_as_predecessor(latest):
            outcomes[nid] = _Outcome(status="succeeded", task=latest)
            continue
        if latest.status == "succeeded":
            # Succeeded but superstep/effects not yet committed-and-acknowledged.
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            continue
        if latest.status == "stopped":
            reason = f"task {latest.task_id} (node '{nid}') stopped"
            if isinstance(latest.value, dict):
                raw = latest.value.get("reason")
                if isinstance(raw, str) and raw.strip():
                    reason = raw
            elif isinstance(latest.value, str) and latest.value.strip():
                reason = latest.value
            return outcomes, retry, f"__graph_stop__:{reason}"
        if latest.status == "interrupted":
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            if _node_interrupt_resolved(projection, nid):
                retry.append(
                    _build_task(
                        compiled,
                        graph,
                        projection,
                        context,
                        nid,
                        max(len(node_tasks) - 1, 0),
                    )
                )
            continue
        if latest.status == "running":
            # wave 仍在飞行：交由 lease/scheduler 对账，planner 不重复执行。
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            continue
        if latest.status == "pending" and latest.next_retry_at is None:
            # Non-deferral pending remains in-flight. Scheduling deferrals always
            # stamp next_retry_at and are reselected below without attempt credit.
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            continue
        if latest.status == "pending" and latest.next_retry_at is not None:
            # D13 lock deferral: durable scheduling state, not a failed attempt.
            # Reselect the same task_id; scheduler honors next_retry_at / attempt 1.
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            retry.append(
                _build_task(
                    compiled,
                    graph,
                    projection,
                    context,
                    nid,
                    max(len(node_tasks) - 1, 0),
                )
            )
            continue
        policy = _retry_policy(compiled, definition)
        if latest.status == "failed":
            exhausted = latest.attempts_used >= policy.max_attempts
            terminal_for_policy = exhausted or latest.error_kind not in policy.retry_on
            retryable = (
                latest.error_kind is not None and latest.error_kind in policy.retry_on and not exhausted
            )
            if not retryable:
                recovery_def = definition.recover
                if (
                    terminal_for_policy
                    and recovery_def is not None
                    and latest.error_kind is not None
                    and latest.error_kind in recovery_def.errors
                ):
                    persisted = projection.recoveries.get(latest.task_id)
                    if persisted is not None:
                        if (
                            persisted.task_id != latest.task_id
                            or persisted.node_id != nid
                            or persisted.generation_ordinal != (latest.generation_ordinal or 0)
                            or persisted.error_kind != latest.error_kind
                            or persisted.message != (latest.error or "task failed")
                            or persisted.via != recovery_def.via
                            or persisted.continue_to != recovery_def.continue_to
                        ):
                            return (
                                outcomes,
                                retry,
                                f"recovery projection for task {latest.task_id} does not match "
                                "the failed task or compiled recovery route",
                            )
                        recovery_event = TaskRecoveryRoutedEvent(
                            type="task_recovery_routed",
                            invocation_id=projection.invocation_id,
                            checkpoint_ns=projection.checkpoint_ns,
                            graph_id=graph.graph_id,
                            node_id=persisted.node_id,
                            generation_ordinal=persisted.generation_ordinal,
                            task_id=persisted.task_id,
                            error_kind=persisted.error_kind,
                            message=persisted.message,
                            via=persisted.via,
                            continue_to=persisted.continue_to,
                        )
                    else:
                        recovery_event = TaskRecoveryRoutedEvent(
                            type="task_recovery_routed",
                            invocation_id=projection.invocation_id,
                            checkpoint_ns=projection.checkpoint_ns,
                            graph_id=graph.graph_id,
                            node_id=nid,
                            generation_ordinal=latest.generation_ordinal or 0,
                            task_id=latest.task_id,
                            error_kind=latest.error_kind,
                            message=latest.error or "task failed",
                            via=recovery_def.via,
                            continue_to=recovery_def.continue_to,
                        )
                    previous_task_id = recovery_vias.get(recovery_event.via)
                    if previous_task_id is not None and previous_task_id != recovery_event.task_id:
                        return (
                            outcomes,
                            retry,
                            f"recovery via node '{recovery_event.via}' received multiple failed sources",
                        )
                    recovery_vias[recovery_event.via] = recovery_event.task_id
                    outcomes[nid] = _Outcome(
                        status="unresolved",
                        task=latest,
                        recovery_event=recovery_event,
                    )
                    continue
                detail = f": {latest.error}" if latest.error else ""
                return (
                    outcomes,
                    retry,
                    (
                        f"task {latest.task_id} (node '{nid}') failed with "
                        f"{latest.error_kind}; attempts {latest.attempts_used}/"
                        f"{policy.max_attempts}{detail}"
                    ),
                )
        elif latest.attempts_used >= policy.max_attempts:  # abandoned
            return (
                outcomes,
                retry,
                (
                    f"task {latest.task_id} (node '{nid}') abandoned; retry budget "
                    f"exhausted ({latest.attempts_used}/{policy.max_attempts})"
                ),
            )
        # failed-retryable 或 abandoned 且预算未耗尽：同一 task_id 进入下一 wave。
        outcomes[nid] = _Outcome(status="unresolved", task=latest)
        retry.append(
            _build_task(
                compiled,
                graph,
                projection,
                context,
                nid,
                len(node_tasks) - 1,
                prior_failure=latest.error if latest.status == "failed" else None,
                prior_error_kind=latest.error_kind if latest.status == "failed" else None,
            )
        )
    retry = _overlay_imported_outcomes(graph, projection, context, outcomes, retry)
    return outcomes, retry, None


def _recovery_metadata(
    projection: GraphProjection,
    outcomes: Mapping[str, _Outcome],
) -> tuple[list[TaskRecoveryRoutedEvent], dict[str, _RecoveryDelivery]]:
    """Extract recovery effects after runtime's imported-outcome overlay wrapper."""
    events: list[TaskRecoveryRoutedEvent] = []
    deliveries: dict[str, _RecoveryDelivery] = {}
    for outcome in outcomes.values():
        event = outcome.recovery_event
        source = outcome.task
        if event is None or source is None:
            continue
        deliveries[event.via] = _RecoveryDelivery(event=event, source=source)
        if event.task_id not in projection.recoveries:
            events.append(event)
    events.sort(key=lambda event: (event.node_id, event.task_id))
    return events, deliveries


def _attach_recovery_context(
    task: ExecutableTask,
    deliveries: Mapping[str, _RecoveryDelivery],
) -> ExecutableTask:
    recovery = deliveries.get(task.node_id)
    if recovery is None:
        return task
    event = recovery.event
    return task.model_copy(
        update={
            "recovery": RecoveryContext(
                source_task_id=event.task_id,
                source_node_id=event.node_id,
                generation_ordinal=event.generation_ordinal,
                error_kind=event.error_kind,
                message=event.message,
                attempts_used=recovery.source.attempts_used,
                recovery_event=event,
            )
        }
    )


def _deliver_tokens(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    scope: Scope,
    outcomes: dict[str, _Outcome],
    recovery_deliveries: Mapping[str, _RecoveryDelivery],
) -> _Delivery:
    """求值所有已成功 node 的出边与 route，冻结选中 token 与终局标记。

    被 skip 的 node 不遍历出边。route 的缺失/MISSING select 无 default 时
    fail closed 到 STOP；edge/route 显式选中 FAIL 是 runtime FAIL 终局。
    join node 的 token 由其声明的 join.sources 表达，镜像 edge/route 不投递。
    budget 消耗点在投递前先查 ledger 投影计数：耗尽即把 token 改道
    ``exhausted_to``（可沿 exhausted_to 链前进），consumer 自身不再被选中。
    """
    selected: dict[str, list[str]] = {}
    rerouted: dict[str, str] = {}
    active_recovery_vias = frozenset(recovery_deliveries)
    end_reached = False
    stop_reason: str | None = None
    fail_reason: str | None = None

    def deliver(target: str, descriptor: str) -> None:
        nonlocal end_reached, stop_reason, fail_reason
        if target == "END":
            end_reached = True
            return
        if target == "STOP":
            if stop_reason is None:
                stop_reason = f"selected STOP via {descriptor}"
            return
        if target == "FAIL":
            if fail_reason is None:
                fail_reason = f"selected FAIL via {descriptor}"
            return
        if target not in graph.nodes or graph.nodes[target].definition.join is not None:
            return
        redirect = _budget_reroute(compiled, graph, projection, target)
        if redirect is None:
            selected.setdefault(target, []).append(descriptor)
            return
        rerouted.setdefault(target, redirect)
        deliver(redirect, f"budget:{target}:exhausted")

    # Phase 1: START + ordinary edges（cycle 回边按成功代数门控，避免 pass 后仍被 fix 拉回）。
    for nid in graph.declaration_order:
        for edge in graph.nodes[nid].incoming:
            if edge.from_ != "START":
                continue
            if _node_has_task(projection, edge.to):
                continue
            if edge.when is None or _satisfied(edge.when, scope, edge.to):
                deliver(edge.to, "edge:START")
    for nid in graph.declaration_order:
        if outcomes[nid].status != "succeeded" or nid in active_recovery_vias:
            continue
        for edge in graph.nodes[nid].outgoing:
            if edge.when is not None and not _satisfied(edge.when, scope, nid):
                continue
            if not _should_deliver_successorship(graph, projection, outcomes, nid, edge.to):
                continue
            deliver(edge.to, f"edge:{nid}")

    # Phase 2: 将被回边重新选中的 settled node 标为跨代再激活；它们上一代的 route 作废。
    reopen = {
        nid
        for nid, status in ((n, outcomes[n].status) for n in graph.nodes)
        if status in ("succeeded", "skipped") and _fresh_cycle_tokens(selected.get(nid, []))
    }

    # Phase 3: routes（跳过即将再激活的 src，避免 stale needs_fix 再次拉起 fix）。
    for nid in graph.declaration_order:
        if outcomes[nid].status != "succeeded" or nid in reopen or nid in active_recovery_vias:
            continue
        for route in graph.nodes[nid].routes:
            try:
                target, descriptor = _resolve_route(route, scope, nid)
            except _StopResolution as exc:
                if stop_reason is None:
                    stop_reason = str(exc)
                continue
            if target not in ("END", "STOP", "FAIL") and not _should_deliver_successorship(
                graph, projection, outcomes, nid, target
            ):
                continue
            deliver(target, descriptor)

    # Recovery routes are ledger-pinned control flow, not ordinary edges. The
    # failed source never succeeds, so only the dedicated via task may release
    # the frozen continuation.
    for via, recovery in sorted(recovery_deliveries.items()):
        if outcomes[via].status == "succeeded" and (
            recovery.event.continue_to not in graph.nodes
            or not _node_has_task_or_settled_generation(
                projection,
                graph.graph_id,
                recovery.event.continue_to,
            )
        ):
            deliver(
                recovery.event.continue_to,
                f"recovery:{recovery.event.task_id}:{via}",
            )
        elif outcomes[via].task is None:
            deliver(via, f"recovery:{recovery.event.task_id}")

    return _Delivery(
        selected=selected,
        end_reached=end_reached,
        stop_reason=stop_reason,
        fail_reason=fail_reason,
        rerouted=rerouted,
    )


# ---------------------------------------------------------------------------
# activation 决策


def _decide_nodes(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    scope: Scope,
    source_reads: dict[str, str],
    delivery: _Delivery,
    outcomes: dict[str, _Outcome],
    recovery_deliveries: Mapping[str, _RecoveryDelivery],
    events: list[BaseModel],
    ready: list[ExecutableTask],
) -> None:
    """按拓扑序对每个尚无当前代结局的 node 做恰好一次激活/跳过决策。"""
    dedicated_recovery_nodes = {
        node.definition.recover.via for node in graph.nodes.values() if node.definition.recover is not None
    }
    recovery_continuation_sources: dict[str, list[str]] = {}
    for source_id, node in graph.nodes.items():
        recovery = node.definition.recover
        if recovery is not None and recovery.continue_to in graph.nodes:
            recovery_continuation_sources.setdefault(recovery.continue_to, []).append(source_id)
    for nid in sorted(graph.nodes, key=lambda n: graph.nodes[n].topology_rank):
        cnode = graph.nodes[nid]
        definition = cnode.definition
        tokens = delivery.selected.get(nid, [])
        if nid in dedicated_recovery_nodes and nid not in recovery_deliveries:
            # A dedicated fallback has no ordinary incoming control flow. It is
            # neither activated nor structurally skipped until a failed source
            # creates (or replays) its pinned recovery route.
            continue
        if not tokens and any(
            outcomes[source_id].status == "unresolved"
            for source_id in recovery_continuation_sources.get(nid, [])
        ):
            # A continuation has a synthetic incoming dependency from recovery.
            # Do not freeze a structural skip while its recoverable source (or
            # dedicated fallback) can still deliver the pinned continuation.
            continue
        if outcomes[nid].task is not None and outcomes[nid].status == "unresolved":
            continue  # task 在飞行/重试中：不重放决策
        if outcomes[nid].status in ("succeeded", "skipped"):
            # 跨代再激活：settled node 收到非 START 新 token（cycle 回边 / 新 route），
            # 或（join 不接收投递 token）其 source 已跑出更新的一代。
            if _fresh_cycle_tokens(tokens) or _join_should_reopen(graph, projection, outcomes, nid):
                outcomes[nid] = _Outcome(status="unresolved")
            else:
                continue
        elif outcomes[nid].status != "unresolved":
            continue
        if definition.join is not None:
            _decide_join(
                compiled,
                graph,
                projection,
                context,
                scope,
                source_reads,
                outcomes,
                events,
                ready,
                nid,
            )
            continue

        if not tokens and not _incoming_resolved(graph, outcomes, nid):
            continue  # 前驱尚未稳定：本 superstep 不做决定
        if len(tokens) > 1:
            raise PlanError(
                f"graph '{graph.graph_id}' node '{nid}' is selected by "
                f"{len(tokens)} non-mutually-exclusive incoming paths "
                f"({', '.join(sorted(tokens))}); declare an explicit builtin:join "
                "instead of relying on duplicate activation"
            )
        if not tokens:
            rerouted_to = delivery.rerouted.get(nid)
            reason = (
                f"(budget exhausted -> {rerouted_to})"
                if rerouted_to is not None
                else "(no incoming edge selected)"
            )
            _emit_skip(graph, projection, source_reads, events, nid, reason)
            # Structural skip: no predecessor ever succeeded/delivered a route to
            # this node, so it never had a real chance to activate this
            # generation (e.g. its sole predecessor was itself skipped because an
            # upstream gate routed elsewhere entirely). Mark unreached so a
            # downstream ``all_active`` join can tell this apart from a node that
            # was genuinely reached but opted out via its own ``when``.
            outcomes[nid] = _Outcome(status="skipped", reached=False)
            continue
        if definition.fan_out is not None:
            _decide_fan_out(
                compiled,
                graph,
                projection,
                context,
                scope,
                source_reads,
                outcomes,
                events,
                ready,
                nid,
            )
            continue
        if definition.when is not None and not _satisfied(definition.when, scope, nid):
            _emit_skip(graph, projection, source_reads, events, nid, definition.when)
            outcomes[nid] = _Outcome(status="skipped")
            continue
        ready.append(_activate(compiled, graph, projection, context, source_reads, events, nid, tokens))
        # 激活的 task 本 superstep 才分发：下游看到的是未解决状态，须等下一代。


def _decide_join(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    scope: Scope,
    source_reads: dict[str, str],
    outcomes: dict[str, _Outcome],
    events: list[BaseModel],
    ready: list[ExecutableTask],
    nid: str,
) -> None:
    definition = graph.nodes[nid].definition
    join = definition.join
    if join is None:  # pragma: no cover - 调用方已保证
        raise PlanError(f"node '{nid}' is not a join")
    states = {src: outcomes[src].status for src in join.sources}
    succeeded = sorted(src for src, status in states.items() if status == "succeeded")
    waiting = any(status == "unresolved" for status in states.values())

    if join.mode == "any":
        if not succeeded:
            if waiting:
                return  # 仍有 source 可能成功：等待（cancel_remaining=false 时 sibling 照常 settle）
            _emit_skip(graph, projection, source_reads, events, nid, "(no join source succeeded)")
            outcomes[nid] = _Outcome(status="skipped")
            return
    else:
        if waiting:
            return
        if join.mode == "all_active" and not succeeded:
            if any(outcomes[src].reached for src in join.sources if states[src] == "skipped"):
                raise PlanError(
                    f"graph '{graph.graph_id}' join '{nid}' mode all_active has no "
                    "activated source (all sources skipped); this is a runtime error, "
                    "not an implicit pass"
                )
            # Every source was structurally unreachable this generation (their own
            # predecessor chain never succeeded — e.g. an upstream gate routed the
            # whole branch elsewhere). There was never a real chance for any source
            # to activate, so propagate the same structural skip instead of hard
            # failing; a genuinely dead branch must not crash the graph.
            _emit_skip(
                graph, projection, source_reads, events, nid, "(all join sources structurally unreachable)"
            )
            outcomes[nid] = _Outcome(status="skipped", reached=False)
            return
        # all：每个 source 已 succeeded 或 skipped；all_active：≥1 个已激活 source 成功。
    if definition.when is not None and not _satisfied(definition.when, scope, nid):
        _emit_skip(graph, projection, source_reads, events, nid, definition.when)
        outcomes[nid] = _Outcome(status="skipped")
        return
    descriptor = f"join:{join.mode}:{','.join(join.sources)}"
    ready.append(_activate(compiled, graph, projection, context, source_reads, events, nid, [descriptor]))


# ---------------------------------------------------------------------------
# fan-out：冻结展开、确定性重放与 reduce


_TEMPLATE = re.compile(r"\$\{([^}]+)\}")


def _seed_fan_out(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    node_tasks: list[TaskProjection],
) -> tuple[_Outcome, list[ExecutableTask], str | None]:
    """把冻结 child 的投影结局折叠成 fan-out node 的当前代 outcome。

    返回 ``(outcome, retry_children, fail_reason)``。``completion: all``：每个
    冻结 child succeeded 后 node 才解决；任一 child 缺失/在飞/待重试时保持
    unresolved（成功 child 的写入保持 pending，不发布部分聚合）。失败/耗尽
    child 的重计划按冻结展开重建同一 task_id；非重试失败是 integrity FAIL。
    """
    definition = graph.nodes[nid].definition
    fan_out = definition.fan_out
    if fan_out is None:  # pragma: no cover - 调用方已保证
        raise PlanError(f"node '{nid}' is not a fan-out")
    expansion = projection.fan_out_expansions.get(nid)
    if expansion is None:
        if node_tasks:
            raise PlanError(
                f"graph '{graph.graph_id}' node '{nid}' has task projections but no "
                "frozen fan_out expansion (ledger integrity)"
            )
        return _Outcome(status="unresolved"), [], None
    _validate_expansion_shape(graph, expansion, nid)
    aggregate_id = fan_out_aggregate_task_id(
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        structural_path=projection.structural_path,
        graph_id=graph.graph_id,
        node_id=nid,
        child_count=len(expansion.task_ids),
    )
    frozen_ids = set(expansion.task_ids) | {aggregate_id}
    unknown = sorted(task.task_id for task in node_tasks if task.task_id not in frozen_ids)
    if unknown:
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' has tasks outside the frozen "
            f"fan_out expansion: {', '.join(unknown)}"
        )

    by_id = {task.task_id: task for task in node_tasks}
    policy = _retry_policy(compiled, definition)
    retry: list[ExecutableTask] = []
    missing = 0
    waiting: TaskProjection | None = None
    for index, task_id in enumerate(expansion.task_ids):
        child = by_id.get(task_id)
        if child is None:
            # 冻结后、分发前的崩溃窗口：child 由 _decide_fan_out 按冻结数据重建。
            missing += 1
            continue
        if child.status == "succeeded":
            continue
        if child.status == "pending" and child.next_retry_at is not None:
            # Scheduling deferral: reselect the same fan-out child without budget use.
            retry.append(
                _build_fan_out_task(
                    compiled,
                    graph,
                    projection,
                    context,
                    nid,
                    expansion,
                    index,
                )
            )
            continue
        if child.status in ("running", "pending", "interrupted"):
            if waiting is None:
                waiting = child
            continue
        if child.status == "failed":
            retryable = (
                child.error_kind is not None
                and child.error_kind in policy.retry_on
                and child.attempts_used < policy.max_attempts
            )
            if not retryable:
                return (
                    _Outcome(status="unresolved"),
                    [],
                    (
                        f"fan-out child {child.task_id} (node '{nid}') failed with "
                        f"{child.error_kind}; attempts {child.attempts_used}/{policy.max_attempts}"
                    ),
                )
        elif child.attempts_used >= policy.max_attempts:  # abandoned
            return (
                _Outcome(status="unresolved"),
                [],
                (
                    f"fan-out child {child.task_id} (node '{nid}') abandoned; retry "
                    f"budget exhausted ({child.attempts_used}/{policy.max_attempts})"
                ),
            )
        # failed-retryable 或 abandoned 且预算未耗尽：同一 task_id 进入下一 wave。
        retry.append(
            _build_fan_out_task(
                compiled,
                graph,
                projection,
                context,
                nid,
                expansion,
                index,
                prior_failure=child.error if child.status == "failed" else None,
                prior_error_kind=child.error_kind if child.status == "failed" else None,
            )
        )

    settled = missing == 0 and waiting is None and not retry
    if settled and fan_out.reduce is not None:
        # 全部成功：reducer 契约与 child value 类型在这里 fail-closed 校验。
        _reduce_fan_out(compiled, graph, projection, nid, fan_out.reduce, expansion)
    if settled:
        if not expansion.task_ids:
            key = node_history_key(projection.checkpoint_ns, graph.graph_id, nid)
            history = projection.node_histories.get(key)
            if history is not None:
                gen = history.generations_by_ordinal.get(history.latest_generation_ordinal)
                if gen is not None and gen.outputs_committed:
                    return _Outcome(status="succeeded"), retry, None
            return _Outcome(status="unresolved"), retry, None
        aggregate_id = fan_out_aggregate_task_id(
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            structural_path=projection.structural_path,
            graph_id=graph.graph_id,
            node_id=nid,
            child_count=len(expansion.task_ids),
        )
        aggregate = by_id.get(aggregate_id) or projection.tasks.get(aggregate_id)
        if aggregate is None or aggregate.status != "succeeded":
            if aggregate is not None:
                return _Outcome(status="unresolved", task=aggregate), retry, None
            return _Outcome(status="unresolved"), retry, None
        key = node_history_key(projection.checkpoint_ns, graph.graph_id, nid)
        history = projection.node_histories.get(key)
        if history is not None:
            gen = history.generations_by_ordinal.get(history.latest_generation_ordinal)
            if gen is None or not gen.outputs_committed:
                return _Outcome(status="unresolved", task=aggregate), retry, None
        return _Outcome(status="succeeded"), retry, None
    if missing:
        representative = None  # 有缺失 child：交给 _decide_fan_out 重放/补建
    elif waiting is not None:
        representative = waiting
    else:
        first_id = expansion.task_ids[0] if expansion.task_ids else None
        representative = by_id.get(first_id) if first_id is not None else None
    return _Outcome(status="unresolved", task=representative), retry, None


def _decide_fan_out(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    scope: Scope,
    source_reads: dict[str, str],
    outcomes: dict[str, _Outcome],
    events: list[BaseModel],
    ready: list[ExecutableTask],
    nid: str,
) -> None:
    """fan-out node 的激活决策：首次冻结展开，或按冻结数据确定性重放。"""
    definition = graph.nodes[nid].definition
    fan_out = definition.fan_out
    if fan_out is None:  # pragma: no cover - 调用方已保证
        raise PlanError(f"node '{nid}' is not a fan-out")
    expansion = projection.fan_out_expansions.get(nid)
    if expansion is not None:
        _validate_expansion_shape(graph, expansion, nid)
        present = [task_id for task_id in expansion.task_ids if task_id in projection.tasks]
        if not present:
            # 展开已冻结但没有任何 child 进入 ledger：重放前对 source hash fail
            # closed；一旦任一 child 落账，child 集合即由 ledger 确认，不再检查。
            _check_fan_out_drift(graph, expansion, source_reads, nid)
        for index, task_id in enumerate(expansion.task_ids):
            if task_id not in projection.tasks:
                ready.append(_build_fan_out_task(compiled, graph, projection, context, nid, expansion, index))
        if all(
            (child := projection.tasks.get(task_id)) is not None and child.status == "succeeded"
            for task_id in expansion.task_ids
        ):
            aggregate_id = fan_out_aggregate_task_id(
                invocation_id=projection.invocation_id,
                checkpoint_ns=projection.checkpoint_ns,
                structural_path=projection.structural_path,
                graph_id=graph.graph_id,
                node_id=nid,
                child_count=len(expansion.task_ids),
            )
            aggregate = projection.tasks.get(aggregate_id)
            if aggregate is None or aggregate.status not in ("succeeded", "running"):
                ready.append(
                    _build_fan_out_aggregate_task(compiled, graph, projection, context, nid, expansion)
                )
        return
    if definition.when is not None and not _satisfied(definition.when, scope, nid):
        _emit_skip(graph, projection, source_reads, events, nid, definition.when)
        outcomes[nid] = _Outcome(status="skipped")
        return
    expansion, children = _expand_fan_out(compiled, graph, projection, context, scope, source_reads, nid)
    # 展开必须先于任何 child 启动冻结在严格事务中（runtime 先持久化事件再分发）。
    events.append(
        FanOutExpandedEvent(
            type="fan_out_expanded",
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            graph_id=graph.graph_id,
            node_id=nid,
            source_reads_sha256=dict(sorted(expansion.source_reads_sha256.items())),
            items=list(expansion.items),
            task_keys=list(expansion.task_keys),
            task_ids=list(expansion.task_ids),
        )
    )
    ready.extend(children)


def _expand_fan_out(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    scope: Scope,
    source_reads: dict[str, str],
    nid: str,
) -> tuple[FanOutExpansion, list[ExecutableTask]]:
    """求值 ``items``、派生唯一 key 与 structural child ID，并展开/校验每个 child。"""
    definition = graph.nodes[nid].definition
    fan_out = definition.fan_out
    if fan_out is None:  # pragma: no cover - 调用方已保证
        raise PlanError(f"node '{nid}' is not a fan-out")
    if fan_out.reduce is not None:
        _reduce_state_def(compiled, graph, nid, fan_out.reduce)
    try:
        value = evaluate(parse_expression(fan_out.items), scope)
    except DslError as exc:
        raise PlanError(f"node '{nid}' fan_out items failed to evaluate: {exc}") from exc
    if not isinstance(value, list):
        raise PlanError(f"node '{nid}' fan_out items must resolve to a list, got {type(value).__name__}")
    if len(value) > fan_out.max_items:
        raise PlanError(
            f"node '{nid}' fan_out expanded {len(value)} items, more than max_items {fan_out.max_items}"
        )
    items = list(value)
    for item in items:
        _canonical_json(item, what=f"node '{nid}' fan_out item")

    keys: list[str] = []
    task_ids: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        resolved_key = _resolve_key(fan_out, item, context, nid)
        canonical_key = _canonical_json(resolved_key, what=f"node '{nid}' fan_out key")
        if canonical_key in seen:
            raise PlanError(f"node '{nid}' fan_out duplicate key {canonical_key}; keys must be unique")
        seen.add(canonical_key)
        # display key 只用于展示/prompt；structural ID 用 canonical key hash。
        keys.append(resolved_key if isinstance(resolved_key, str) else canonical_key)
        task_ids.append(_task_id(projection, graph.graph_id, nid, index, canonical_key))
    expansion = FanOutExpansion(
        items=tuple(items),
        task_keys=tuple(keys),
        task_ids=tuple(task_ids),
        source_reads_sha256=dict(sorted(source_reads.items())),
    )
    # 先完整展开并校验每个 child（模板白名单 + 路径安全），再允许冻结事件产生。
    children = [
        _build_fan_out_task(compiled, graph, projection, context, nid, expansion, index)
        for index in range(len(items))
    ]
    return expansion, children


def _resolve_template(
    template: str,
    item_as: str,
    item: object,
    context: RuntimeContext,
    nid: str,
    *,
    path: bool,
) -> object:
    """解析 item、item mapping 字段、context 与 params 模板；其它变量一律拒绝。

    整串恰好一个模板时返回原值（标量/结构化 item 均可）；复合串把各模板替换为
    display 字符串。``path=True`` 时每个替换值必须是安全 path segment。
    """
    matches = list(_TEMPLATE.finditer(template))
    for match in matches:
        var = match.group(1)
        if var.startswith("params."):
            pname = var.removeprefix("params.")
            value = context.params.get(pname)
            if not isinstance(value, str) or not value.strip():
                raise PlanError(f"node '{nid}' template '${{{var}}}' requires a non-empty string param")
            if pname.endswith("sha256") and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                continue
            try:
                assert_path_segment_safe(value, label=f"params.{pname}")
            except UnsafeIdentifierError as err:
                raise PlanError(f"node '{nid}' template '${{{var}}}': {err}") from err
            continue
        is_item_field = var.startswith(f"{item_as}.") and len(var) > len(item_as) + 1
        if var not in (item_as, "context.change_id") and not is_item_field:
            raise PlanError(f"node '{nid}' template '${{{var}}}' is not an allowed item/context template")
    if not matches:
        return template

    def value_of(var: str) -> object:
        if var.startswith("params."):
            return context.params[var.removeprefix("params.")]
        if var.startswith(f"{item_as}."):
            field = var.removeprefix(f"{item_as}.")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                raise PlanError(f"node '{nid}' fan_out item field is unsafe: {field!r}")
            if not isinstance(item, Mapping) or field not in item:
                raise PlanError(f"node '{nid}' fan_out item has no field {field!r}")
            return item[field]
        return item if var == item_as else context.change_id

    if len(matches) == 1 and matches[0].span() == (0, len(template)):
        value = value_of(matches[0].group(1))
        if not path:
            return value
        display = _display(value, nid)
        _assert_safe_segment(display, nid)
        return display

    def substitute(match: re.Match[str]) -> str:
        display = _display(value_of(match.group(1)), nid)
        if path:
            _assert_safe_segment(display, nid)
        return display

    return _TEMPLATE.sub(substitute, template)


def _display(value: object, nid: str) -> str:
    """标量的 display 字符串化；非标量不能插值进字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _canonical_json(value, what=f"node '{nid}' fan_out template value")
    raise PlanError(
        f"node '{nid}' fan_out template value must be a scalar to interpolate "
        f"into a string, got {type(value).__name__}"
    )


def _assert_safe_segment(display: str, nid: str) -> None:
    if display in ("", ".", "..") or "/" in display or "\\" in display:
        raise PlanError(f"node '{nid}' fan_out expansion has unsafe path segment {display!r}")


def _assert_safe_path(path: str, nid: str) -> None:
    if not path.startswith(("change:", "project:", "repo:")):
        raise PlanError(
            f"node '{nid}' fan_out expansion path '{path}' must stay rooted in change:/project:/repo:"
        )
    rest = path.partition(":")[2]
    segments = rest.split("/")
    body = segments[:-1] if segments and segments[-1] == "" else segments  # 目录 output 允许结尾 "/"
    if rest.startswith("/") or "\\" in rest or any(seg in ("", ".", "..") for seg in body):
        raise PlanError(f"node '{nid}' fan_out expansion produces unsafe path '{path}'")


def _resolve_key(
    fan_out: FanOutDef,
    item: object,
    context: RuntimeContext,
    nid: str,
) -> str | int | float | bool:
    resolved = _resolve_template(fan_out.key, fan_out.item_as, item, context, nid, path=False)
    if not isinstance(resolved, (str, int, float, bool)):
        raise PlanError(
            f"node '{nid}' fan_out key must resolve to str | int | float | bool, "
            f"got {type(resolved).__name__}"
        )
    return resolved


def _expand_templates(
    value: object,
    item_as: str,
    item: object,
    context: RuntimeContext,
    nid: str,
) -> object:
    """递归展开 ``with`` 值中的模板（dict key 是标识符，不做插值）。"""
    if isinstance(value, str):
        return _resolve_template(value, item_as, item, context, nid, path=False)
    if isinstance(value, list):
        return [_expand_templates(entry, item_as, item, context, nid) for entry in value]
    if isinstance(value, dict):
        return {key: _expand_templates(entry, item_as, item, context, nid) for key, entry in value.items()}
    return value


def _expand_output(
    output: str,
    item_as: str,
    item: object,
    context: RuntimeContext,
    nid: str,
) -> str:
    resolved = _resolve_template(output, item_as, item, context, nid, path=True)
    if not isinstance(resolved, str):
        raise PlanError(f"node '{nid}' fan_out output must resolve to a string path")
    _assert_safe_path(resolved, nid)
    return resolved


def _expand_static_output(output: str, context: RuntimeContext, nid: str) -> str:
    """Expand ``${params.<name>}`` and ``${context.change_id}`` in a non-fan-out output path.

    Regular (non-fan-out) node outputs may carry ``${params.X}`` segments so the
    same schema works across different param values (e.g. ``retro_id``).  Fan-out
    expansion already routes through ``_expand_output``; this helper handles the
    equivalent substitution for singleton tasks.
    """
    resolved = _resolve_template(output, "__none__", None, context, nid, path=True)
    if not isinstance(resolved, str):
        raise PlanError(f"node '{nid}' output template must resolve to a string path")
    _assert_safe_path(resolved, nid)
    return resolved


def _expand_resources(
    resources: ResourceDef,
    item_as: str,
    item: object,
    context: RuntimeContext,
    nid: str,
) -> dict[str, list[str]]:
    """展开 node 显式 resource claim 中的模板并校验展开后 claim 的合法性。

    展开结果随 task input 传递；逐 child 收窄与 contract 授权校验随
    scheduler/handler 侧的 contract 注入演进（Task 9/10）。
    """
    expanded: dict[str, list[str]] = {
        "reads": [],
        "writes": [],
        "synchronized": [],
        "exclusive": [],
    }
    for field in ("reads", "writes", "synchronized"):
        for claim in getattr(resources, field):
            resolved = _resolve_template(claim, item_as, item, context, nid, path=True)
            if not isinstance(resolved, str):
                raise PlanError(f"node '{nid}' fan_out resource claim must resolve to a string")
            try:
                ResourcePath.parse(resolved)
            except ContractError as exc:
                raise PlanError(f"node '{nid}' fan_out expansion: {exc}") from exc
            expanded[field].append(resolved)
    for token in resources.exclusive:
        resolved = _resolve_template(token, item_as, item, context, nid, path=False)
        display = resolved if isinstance(resolved, str) else _display(resolved, nid)
        if not display:
            raise PlanError(f"node '{nid}' fan_out expansion has empty exclusive token")
        expanded["exclusive"].append(display)
    return expanded


def _reduce_state_def(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    nid: str,
    reduce: ReduceDef,
) -> StateDef:
    """reducer 目标/类型契约：into 必须声明，using 必须与 state 类型匹配。"""
    state_def = compiled.schema.graphs[graph.graph_id].state.get(reduce.into)
    if state_def is None:
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' fan_out reduce targets "
            f"undeclared state key '{reduce.into}'"
        )
    if reduce.using in ("append", "set_union") and state_def.type != "list":
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' fan_out reducer '{reduce.using}' "
            f"requires list state '{reduce.into}', got type '{state_def.type}'"
        )
    if reduce.using == "merge_disjoint" and state_def.type != "object":
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' fan_out reducer 'merge_disjoint' "
            f"requires object state '{reduce.into}', got type '{state_def.type}'"
        )
    return state_def


def _reduce_fan_out(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    nid: str,
    reduce: ReduceDef,
    expansion: FanOutExpansion,
) -> object:
    """把 child value 按冻结 item 序（而非完成先后）喂给声明的 reducer。"""
    state_def = _reduce_state_def(compiled, graph, nid, reduce)
    reduced = state_def.default
    for task_id in expansion.task_ids:
        child = projection.tasks[task_id]  # 调用方保证存在且 succeeded
        reduced = _reduce(reduce.using, reduced, child.value)
    return reduced


def fan_out_state_updates(
    compiled: CompiledWorkflow,
    projection: GraphProjection,
) -> list[tuple[str, dict[str, object]]]:
    """已完成 fan-out node 的 reduced state update（冻结 item 序），供 Update 合并。

    只在每个冻结 child 都 succeeded 后产出；任一 child failed/retrying/
    interrupted/pending 时该 node 不产出——成功 child 的写入保持 pending，
    不发布部分聚合。返回 ``(update_id, {state_key: reduced})`` 供
    ``apply_state_updates`` 与其它 task update 一起确定性合并。
    """
    graph = _resolve_graph(compiled, projection)
    updates: list[tuple[str, dict[str, object]]] = []
    for nid in graph.declaration_order:
        definition = graph.nodes[nid].definition
        fan_out = definition.fan_out
        if fan_out is None or fan_out.reduce is None:
            continue
        expansion = projection.fan_out_expansions.get(nid)
        if expansion is None:
            continue
        _validate_expansion_shape(graph, expansion, nid)
        children = [projection.tasks.get(task_id) for task_id in expansion.task_ids]
        if any(child is None or child.status != "succeeded" for child in children):
            continue
        reduced = _reduce_fan_out(compiled, graph, projection, nid, fan_out.reduce, expansion)
        updates.append((f"reduce:{projection.structural_path}:{nid}", {fan_out.reduce.into: reduced}))
    return updates


def _validate_expansion_shape(graph: CompiledGraph, expansion: FanOutExpansion, nid: str) -> None:
    if not (len(expansion.items) == len(expansion.task_keys) == len(expansion.task_ids)):
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' fan_out expansion is malformed: "
            f"{len(expansion.items)} items, {len(expansion.task_keys)} keys, "
            f"{len(expansion.task_ids)} task ids (ledger integrity)"
        )


def _check_fan_out_drift(
    graph: CompiledGraph,
    expansion: FanOutExpansion,
    source_reads: dict[str, str],
    nid: str,
) -> None:
    """重放前把当前 source-read hash 与冻结的 ``source_reads_sha256`` 比对。"""
    frozen = dict(sorted(expansion.source_reads_sha256.items()))
    current = dict(sorted(source_reads.items()))
    if frozen != current:
        raise PlanError(
            f"fan_out_source_drift: graph '{graph.graph_id}' node '{nid}' source "
            "reads drifted from the frozen expansion; never recompute a different "
            "item list"
        )


def _fan_out_child_order(
    projection: GraphProjection,
    events: Sequence[BaseModel],
) -> dict[str, int]:
    """child task_id -> 冻结 item 序下标（投影中的展开 + 本批新展开）。"""
    order: dict[str, int] = {}
    for expansion in projection.fan_out_expansions.values():
        for index, task_id in enumerate(expansion.task_ids):
            order[task_id] = index
    for event in events:
        if isinstance(event, FanOutExpandedEvent):
            for index, task_id in enumerate(event.task_ids):
                order.setdefault(task_id, index)
    return order


# ---------------------------------------------------------------------------
# business budget：ledger 权威计数、耗尽改道与 consumption 标记


def _budget_limit(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    nid: str,
    budget: BudgetUseDef,
) -> int:
    """解析 budget limit：非负 int 字面量或 ``params.<int>`` 引用（fail closed）。"""
    budget_def = compiled.schema.graphs[graph.graph_id].budgets.get(budget.consume)
    if budget_def is None:  # compiler 已拒绝；防御性 fail closed
        raise PlanError(f"graph '{graph.graph_id}' node '{nid}' consumes unknown budget '{budget.consume}'")
    limit = budget_def.limit
    if isinstance(limit, bool):
        raise PlanError(
            f"graph '{graph.graph_id}' budget '{budget.consume}' limit must be a non-negative int"
        )
    if isinstance(limit, int):
        if limit < 0:
            raise PlanError(f"graph '{graph.graph_id}' budget '{budget.consume}' limit must be non-negative")
        return limit
    try:
        expr = parse_expression(limit)
    except DslError as exc:
        raise PlanError(
            f"graph '{graph.graph_id}' budget '{budget.consume}' limit: invalid expression — {exc}"
        ) from exc
    if isinstance(expr, Member) and isinstance(expr.obj, Ident) and expr.obj.name == "params":
        value = projection.params.get(expr.prop)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PlanError(
                f"graph '{graph.graph_id}' budget '{budget.consume}' limit param "
                f"'{expr.prop}' must resolve to a non-negative int"
            )
        return value
    raise PlanError(
        f"graph '{graph.graph_id}' budget '{budget.consume}' limit must be a "
        "non-negative int or a params.<int> reference"
    )


def _budget_reroute(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    nid: str,
) -> str | None:
    """budget 耗尽时返回应接收 token 的目标；未耗尽返回 None。

    沿 ``exhausted_to`` 链前进（链上 consumer 也可能已耗尽）；编译器保证
    exhausted_to 离开 SCC，环检测是防御性兜底。只读 ledger 投影计数，
    绝不读 checkpoint 缓存或 workflow-state.yaml。
    """
    seen: set[str] = set()
    current = nid
    exhausted = False
    while True:
        budget = graph.nodes[current].definition.budget
        if budget is not None:
            limit = _budget_limit(compiled, graph, projection, current, budget)
            if projection.budgets.get(budget.consume, 0) >= limit:
                exhausted = True
                if current in seen:
                    raise PlanError(f"graph '{graph.graph_id}' budget exhausted_to cycle at node '{current}'")
                seen.add(current)
                target = budget.exhausted_to
                if target not in graph.nodes:
                    return target  # END/STOP/FAIL
                current = target
                continue
        return current if exhausted else None


def _budget_mark(
    definition: NodeDef,
    projection: GraphProjection,
    task_id: str,
) -> BudgetConsumption | None:
    """给 budget consumer task 打标记；scheduler 据此原子 staging success + 预算事件。"""
    if definition.budget is None:
        return None
    return BudgetConsumption(
        budget_id=definition.budget.consume,
        consumption_id=canonical_digest(
            {
                "invocation_id": projection.invocation_id,
                "checkpoint_ns": projection.checkpoint_ns,
                "budget_id": definition.budget.consume,
                "task_id": task_id,
            }
        ),
    )


# ---------------------------------------------------------------------------
# 事件与 task 构建


def _next_generation_ordinal(projection: GraphProjection, graph_id: str, node_id: str) -> int:
    """spec §5.3 的代号分配器：``latest + 1``（无历史则 0）。

    尚未接入 ``_activate``/``_emit_skip``：planner 会在后续 superstep 幂等重发
    同一决策（同 ``activation_id``/同 skip 表达式），逐次 +1 会把已成功的代挤到
    非最新槽位，使 ``node(id).outputs`` 读到空代。接线前需先按 §5.4 判定「本次
    决策是否已有槽位」并复用之。
    """
    history = projection.node_histories.get(node_history_key(projection.checkpoint_ns, graph_id, node_id))
    if history is None:
        return 0
    return history.latest_generation_ordinal + 1


def _activate(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    source_reads: dict[str, str],
    events: list[BaseModel],
    nid: str,
    tokens: list[str],
) -> ExecutableTask:
    ordinal = sum(1 for task in projection.tasks.values() if task.node_id == nid)
    task = _build_task(compiled, graph, projection, context, nid, ordinal)
    events.append(
        NodeActivatedEvent(
            type="node_activated",
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            graph_id=graph.graph_id,
            node_id=nid,
            generation_ordinal=ordinal,
            activation_id=canonical_digest(
                {
                    "invocation_id": projection.invocation_id,
                    "checkpoint_ns": projection.checkpoint_ns,
                    "graph_id": graph.graph_id,
                    "node_id": nid,
                    "ordinal": ordinal,
                }
            ),
            input_sha256=canonical_digest({"tokens": sorted(tokens), "task_input_sha256": task.input_sha256}),
            source_reads_sha256=dict(sorted(source_reads.items())),
        )
    )
    return task


def _emit_skip(
    graph: CompiledGraph,
    projection: GraphProjection,
    source_reads: dict[str, str],
    events: list[BaseModel],
    nid: str,
    expression: str,
) -> None:
    events.append(
        NodeSkippedEvent(
            type="node_skipped",
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            graph_id=graph.graph_id,
            node_id=nid,
            expression=expression,
            input_sha256=canonical_digest({"expression": expression, "tokens": []}),
            source_reads_sha256=dict(sorted(source_reads.items())),
        )
    )


def _narrow_task_resources(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    nid: str,
    *,
    expanded_resources: dict[str, list[str]] | None,
    expanded_outputs: list[str],
) -> ResourceClaims:
    """Apply ``narrow_claims`` when the node declares concrete resource templates."""
    base = _task_resources(compiled, graph, nid)
    if expanded_resources is None:
        return base
    try:
        reads = (
            tuple(ResourcePath.parse(value) for value in expanded_resources["reads"])
            if expanded_resources["reads"]
            else base.reads
        )
        writes = tuple(ResourcePath.parse(value) for value in expanded_resources["writes"])
        synchronized = (
            tuple(ResourcePath.parse(value) for value in expanded_resources["synchronized"])
            if expanded_resources["synchronized"]
            else None
        )
        outputs = tuple(ResourcePath.parse(value) for value in expanded_outputs)
        return narrow_claims(
            base,
            reads=reads,
            writes=writes,
            outputs=outputs,
            synchronized=synchronized,
        )
    except ContractError as exc:
        raise PlanError(f"node '{nid}' resource narrowing: {exc}") from exc


def _build_task(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    ordinal: int,
    *,
    prior_failure: str | None = None,
    prior_error_kind: ErrorKind | None = None,
) -> ExecutableTask:
    definition = graph.nodes[nid].definition
    expanded_outputs = [_expand_static_output(o, context, nid) for o in definition.outputs]
    input_payload: dict[str, object] = {
        "with": dict(definition.with_),
        "context": {"change_id": context.change_id},
        "outputs": expanded_outputs,
    }
    expanded_resources: dict[str, list[str]] | None = None
    if definition.resources is not None:
        expanded_resources = _expand_resources(definition.resources, "__none__", None, context, nid)
        input_payload["resources"] = expanded_resources
    retry_policy = _retry_policy(compiled, definition)
    task_id = _task_id(projection, graph.graph_id, nid, ordinal, None)
    return ExecutableTask(
        task_id=task_id,
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        graph_id=graph.graph_id,
        node_id=nid,
        structural_path=projection.structural_path,
        input=input_payload,
        input_sha256=canonical_digest(input_payload),
        contract_digest=compiled.contract_digests.get(definition.uses, ""),
        retryable_errors=tuple(retry_policy.retry_on),
        retry_policy=retry_policy,
        timeout_policy=_timeout_policy(compiled, definition),
        target=definition.uses,
        resources=_narrow_task_resources(
            compiled,
            graph,
            nid,
            expanded_resources=expanded_resources,
            expanded_outputs=expanded_outputs,
        ),
        task_key=None,
        budget=_budget_mark(definition, projection, task_id),
        prior_failure=prior_failure,
        prior_error_kind=prior_error_kind,
    )


def _build_fan_out_aggregate_task(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    expansion: FanOutExpansion,
) -> ExecutableTask:
    """Fan-out 全部 child 成功后的 synthetic aggregate（side-effect-free）。"""
    task_id = fan_out_aggregate_task_id(
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        structural_path=projection.structural_path,
        graph_id=graph.graph_id,
        node_id=nid,
        child_count=len(expansion.task_ids),
    )
    expected = _task_id(projection, graph.graph_id, nid, len(expansion.task_ids), "__aggregate__")
    if task_id != expected:
        raise PlanError(
            f"node '{nid}' fan_out aggregate task ID drifted from frozen expansion (ledger integrity)"
        )
    definition = graph.nodes[nid].definition
    retry_policy = _retry_policy(compiled, definition)
    input_payload: dict[str, object] = {
        "with": {},
        "context": {"change_id": context.change_id},
        "outputs": [],
        "fan_out_aggregate": True,
    }
    return ExecutableTask(
        task_id=task_id,
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        graph_id=graph.graph_id,
        node_id=nid,
        structural_path=projection.structural_path,
        input=input_payload,
        input_sha256=canonical_digest(input_payload),
        contract_digest=compiled.contract_digests.get("builtin:join", ""),
        retryable_errors=(),
        retry_policy=retry_policy,
        timeout_policy=_timeout_policy(compiled, definition),
        target="builtin:join",
        resources=ResourceClaims(),
        task_key="__aggregate__",
        budget=None,
    )


def _build_fan_out_task(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    expansion: FanOutExpansion,
    index: int,
    *,
    prior_failure: str | None = None,
    prior_error_kind: ErrorKind | None = None,
) -> ExecutableTask:
    """按冻结展开重建第 ``index`` 个 child：同一 ID、同一展开输入，可安全重放。"""
    definition = graph.nodes[nid].definition
    fan_out = definition.fan_out
    if fan_out is None:  # pragma: no cover - 调用方已保证
        raise PlanError(f"node '{nid}' is not a fan-out")
    item = expansion.items[index]
    display_key = expansion.task_keys[index]
    canonical_key = _canonical_json(
        _resolve_key(fan_out, item, context, nid), what=f"node '{nid}' fan_out key"
    )
    task_id = _task_id(projection, graph.graph_id, nid, index, canonical_key)
    if task_id != expansion.task_ids[index]:
        raise PlanError(
            f"node '{nid}' fan_out child {index} task ID drifted from the frozen expansion (ledger integrity)"
        )
    expanded_outputs = [
        _expand_output(output, fan_out.item_as, item, context, nid) for output in definition.outputs
    ]
    input_payload: dict[str, object] = {
        "with": _expand_templates(dict(definition.with_), fan_out.item_as, item, context, nid),
        "context": {"change_id": context.change_id},
        "outputs": expanded_outputs,
        "fan_out": {"item_as": fan_out.item_as, "item": item, "task_key": display_key},
    }
    expanded_resources: dict[str, list[str]] | None = None
    if definition.resources is not None:
        expanded_resources = _expand_resources(definition.resources, fan_out.item_as, item, context, nid)
        input_payload["resources"] = expanded_resources
    retry_policy = _retry_policy(compiled, definition)
    return ExecutableTask(
        task_id=task_id,
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        graph_id=graph.graph_id,
        node_id=nid,
        structural_path=projection.structural_path,
        input=input_payload,
        input_sha256=canonical_digest(input_payload),
        contract_digest=compiled.contract_digests.get(definition.uses, ""),
        retryable_errors=tuple(retry_policy.retry_on),
        retry_policy=retry_policy,
        timeout_policy=_timeout_policy(compiled, definition),
        target=definition.uses,
        resources=_narrow_task_resources(
            compiled,
            graph,
            nid,
            expanded_resources=expanded_resources,
            expanded_outputs=expanded_outputs,
        ),
        task_key=display_key,
        budget=_budget_mark(definition, projection, task_id),
        prior_failure=prior_failure,
        prior_error_kind=prior_error_kind,
    )


# ---------------------------------------------------------------------------
# 小工具


def _task_id(
    projection: GraphProjection,
    graph_id: str,
    nid: str,
    ordinal: int,
    task_key: str | None,
) -> str:
    """structural task ID：invocation + namespace + graph/node + ordinal + fan-out key。

    只含结构成分；wall-clock 与线程完成顺序永不进入 ID。
    """
    return canonical_digest(
        {
            "invocation_id": projection.invocation_id,
            "checkpoint_ns": projection.checkpoint_ns,
            "structural_path": projection.structural_path,
            "graph_id": graph_id,
            "node_id": nid,
            "ordinal": ordinal,
            "task_key": task_key,
        }
    )


def _superstep_id(projection: GraphProjection) -> str:
    return canonical_digest(
        {
            "invocation_id": projection.invocation_id,
            "checkpoint_ns": projection.checkpoint_ns,
            "superstep": projection.supersteps,
        }
    )


def _task_sort_key(
    graph: CompiledGraph,
    task: ExecutableTask,
    child_order: Mapping[str, int],
) -> tuple[int, int, int, str]:
    """拓扑序 → 声明序 → 冻结 item 序（fan-out child）→ task ID。

    fan-out child 按源列表顺序返回，绝不按 task ID 哈希序或完成先后排序。
    """
    cnode = graph.nodes[task.node_id]
    return (
        cnode.topology_rank,
        cnode.declaration_index,
        child_order.get(task.task_id, 0),
        task.task_id,
    )


def _latest_task(
    graph: CompiledGraph,
    projection: GraphProjection,
    nid: str,
    node_tasks: list[TaskProjection],
) -> TaskProjection:
    if len(node_tasks) == 1:
        return node_tasks[0]
    by_id = {task.task_id: task for task in node_tasks}
    for ordinal in range(len(node_tasks) - 1, -1, -1):
        candidate = _task_id(projection, graph.graph_id, nid, ordinal, None)
        if candidate in by_id:
            return by_id[candidate]
    raise PlanError(
        f"cannot determine latest generation of node '{nid}' in invocation "
        f"{projection.invocation_id}: task IDs do not match structural derivation"
    )


def _node_has_task(projection: GraphProjection, nid: str) -> bool:
    return any(task.node_id == nid for task in projection.tasks.values())


def _node_has_task_or_settled_generation(
    projection: GraphProjection,
    graph_id: str,
    nid: str,
) -> bool:
    if _node_has_task(projection, nid):
        return True
    history = projection.node_histories.get(node_history_key(projection.checkpoint_ns, graph_id, nid))
    if history is None or history.latest_generation_ordinal < 0:
        return False
    generation = history.generations_by_ordinal.get(history.latest_generation_ordinal)
    return generation is not None and generation.status in (
        "skipped",
        "succeeded",
        "failed",
        "abandoned",
        "stopped",
    )


def _task_ready_as_predecessor(task: TaskProjection) -> bool:
    """D14: successors require committed superstep and acknowledged effects."""
    if task.status != "succeeded" or not task.outputs_committed:
        return False
    if not task.durable_effects:
        return True
    acked = set(task.acknowledged_effect_ids)
    for raw in task.durable_effects:
        effect_id = raw.get("effect_id")
        if not isinstance(effect_id, str) or effect_id not in acked:
            return False
    return True


def _succeeded_count(projection: GraphProjection, nid: str) -> int:
    """Count non-child successes for successorship gating.

    Fan-out children share ``node_id`` with the parent but must not inflate
    ``succ(parent)``; otherwise upstream edges stop delivering while the node
    is still waiting on the synthetic aggregate.
    """
    expansion = projection.fan_out_expansions.get(nid)
    child_ids = set(expansion.task_ids) if expansion is not None else set()
    return sum(
        1
        for task in projection.tasks.values()
        if task.node_id == nid
        and _task_ready_as_predecessor(task)
        and task.task_id not in child_ids
        and not task.fan_out_child
    )


def _same_cyclic_scc(graph: CompiledGraph, left: str, right: str) -> bool:
    for scc in graph.sccs:
        if left in scc and right in scc:
            return len(scc) > 1 or left == right
    return False


def _is_back_edge(graph: CompiledGraph, src: str, dst: str) -> bool:
    """SCC 内 src→dst 是否为回边（指回环头）。

    以声明序为环头判据：良构工作流按执行顺序声明节点，闭合循环的边（如
    decide→proposal、fix→review）总是指向更早声明的环头（``decl[dst] <= decl[src]``），
    而前向边指向更晚声明的下游。据此区分「重投使环头再跑一代」与「一次性前向推进」。
    """
    return graph.nodes[dst].declaration_index <= graph.nodes[src].declaration_index


def _should_deliver_successorship(
    graph: CompiledGraph,
    projection: GraphProjection,
    outcomes: Mapping[str, _Outcome],
    src: str,
    dst: str,
) -> bool:
    """门控已成功 src 对 dst 的 edge/route token，防止 DAG 重复投递与 cycle 过期回边。

    - DAG（不同 SCC）：``succ(src) > succ(dst)``。
    - cyclic SCC 内的**回边**（dst 声明序 <= src，即指回环头）：
      ``succ(src) >= succ(dst) and succ(src) > 0`` —— fix 追上 review 代数时回边生效，
      让环头再跑一代。
    - cyclic SCC 内的**前向边**（dst 声明序 > src）：按 DAG 语义 ``succ(src) > succ(dst)``。
      否则前向边会在下游已追平（succ 相等）时反复重投，导致 allocate/fixer 空转、
      白烧循环预算，且饿死同代的兄弟节点。
    - fan-out 空展开等「无 task 投影但 outcome 已 succeeded」的 src 按至少 1 次成功计。
    """
    if dst in ("END", "STOP", "FAIL"):
        return True

    def _effective_succ(nid: str) -> int:
        counted = _succeeded_count(projection, nid)
        if counted == 0 and outcomes.get(nid) is not None and outcomes[nid].status == "succeeded":
            return 1
        return counted

    src_n = _effective_succ(src)
    dst_n = _effective_succ(dst)
    if _same_cyclic_scc(graph, src, dst) and _is_back_edge(graph, src, dst):
        return src_n > 0 and src_n >= dst_n
    return src_n > dst_n


def _fresh_cycle_tokens(tokens: Sequence[str]) -> bool:
    """非 START 入边/route/budget token 视为 cycle 跨代再激活信号。"""
    return any(token != "edge:START" for token in tokens)


def _join_should_reopen(
    graph: CompiledGraph,
    projection: GraphProjection,
    outcomes: Mapping[str, _Outcome],
    nid: str,
) -> bool:
    """已 settled 的 join 是否应跨代再激活。

    join 节点不经 edge/route 投递 token（``deliver`` 跳过 join 目标），因此无法靠
    ``_fresh_cycle_tokens`` 感知回边。循环体内的 join 需在其 source 跑出比自身更新的
    一代时重新触发：``max(succ(source)) > succ(join)``。非循环 join 的 source 只成功
    一次，永不满足，故对 DAG 无副作用。``_decide_join`` 随后仍会在 source 未就绪时等待。
    """
    definition = graph.nodes[nid].definition
    join = definition.join
    if join is None:
        return False
    join_n = _succeeded_count(projection, nid)
    return any(_succeeded_count(projection, src) > join_n for src in join.sources)


def _incoming_resolved(graph: CompiledGraph, outcomes: dict[str, _Outcome], nid: str) -> bool:
    """前驱结果是否全部稳定：edge/route 源 succeeded 或 skipped；START 恒已解决。

    budget 消耗点是其 ``exhausted_to`` 目标的潜在 token 源，与 route 源同等处理。
    """
    for edge in graph.nodes[nid].incoming:
        if edge.from_ != "START" and outcomes[edge.from_].status == "unresolved":
            return False
    for other in graph.nodes.values():
        if outcomes[other.node_id].status != "unresolved":
            continue
        if any(nid in route.cases.values() or nid == route.default for route in other.routes):
            return False
        budget = other.definition.budget
        if budget is not None and budget.exhausted_to == nid:
            return False
    return True


def _satisfied(expression: str, scope: Scope, nid: str) -> bool:
    try:
        return is_satisfied(parse_expression(expression), scope)
    except DslError as exc:
        raise PlanError(f"node '{nid}' condition failed to evaluate: {exc}") from exc


def _resolve_route(route: RouteDef, scope: Scope, src: str) -> tuple[str, str]:
    """求值 route select 并解析目标；缺失/MISSING 时 default，否则 fail closed 到 STOP。"""
    try:
        label = evaluate(parse_expression(route.select), scope)
    except DslError as exc:
        raise PlanError(f"route from '{src}' select failed to evaluate: {exc}") from exc
    case_label: str | None = None
    if isinstance(label, bool):
        # YAML/JSON case keys are strings, while DSL predicates naturally return
        # booleans. Keep the wire spelling canonical instead of forcing every
        # producer to turn a boolean decision into presentation text.
        case_label = "true" if label else "false"
    elif label is not MISSING and isinstance(label, str):
        case_label = label
    chosen = route.cases.get(case_label) if case_label is not None else None
    if chosen is None:
        if route.default is not None:
            return route.default, f"route:{src}:default"
        raise _StopResolution(
            f"route from '{src}' select resolved to no declared case and has no "
            "default; failing closed to STOP"
        )
    return chosen, f"route:{src}:{case_label}"


def _retry_policy(compiled: CompiledWorkflow, definition: NodeDef) -> RetryPolicyDef:
    if definition.retry is None:
        return _DEFAULT_RETRY
    policy = compiled.schema.policies.retry.get(definition.retry)
    if policy is None:  # compiler 已拒绝；防御性 fail closed
        raise PlanError(f"unknown retry policy '{definition.retry}'")
    return policy


def _timeout_policy(compiled: CompiledWorkflow, definition: NodeDef) -> TimeoutPolicyDef:
    if definition.timeout is None:
        return _DEFAULT_TIMEOUT
    policy = compiled.schema.policies.timeout.get(definition.timeout)
    if policy is None:  # compiler 已拒绝；防御性 fail closed
        raise PlanError(f"unknown timeout policy '{definition.timeout}'")
    return policy


def _task_resources(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    nid: str,
) -> ResourceClaims:
    """task 的资源 claim：subgraph 目标用子图 footprint，否则用本 node 的 claim。

    逐 node claim 由编译期 ``catalog.claims_for`` 合成（catalog 缺失时为
    ``global:exclusive``），比全图 footprint 并集更窄，让互不冲突的兄弟 node
    可在同一 wave 并行；真正的写/exclusive 冲突仍由 ``claims_conflict`` 串行化。
    """
    node = graph.nodes[nid]
    prefix, _, target = node.definition.uses.partition(":")
    if prefix == "graph" and target in compiled.graphs:
        return compiled.graphs[target].resource_footprint
    return node.resources


def _has_inflight(outcomes: dict[str, _Outcome]) -> bool:
    return any(
        outcome.status == "unresolved"
        and outcome.task is not None
        and (
            outcome.task.status in ("running", "interrupted")
            or (outcome.task.status == "pending" and outcome.task.next_retry_at is None)
        )
        for outcome in outcomes.values()
    )


__all__ = [
    "PlanError",
    "PlanErrorKind",
    "apply_state_updates",
    "fan_out_state_updates",
    "plan_superstep",
]
