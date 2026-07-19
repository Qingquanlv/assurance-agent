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

当前边界（后续 task 扩展，均在 planner.py 内演进）：cyclic SCC 的跨代再激活
（activation ordinal > 0 的新 token 代）随 Task 11 落地，本实现完整支持 DAG
与单代语义；嵌套 subgraph invocation 的 graph 解析属于 Task 12，这里只接受
root invocation（经 entrypoint 解析 graph）。逐 child 收窄 resource claims 与
contract 授权范围校验随 scheduler/handler 侧的 contract 注入演进（Task
9/10）；planner 只对展开后的路径做 root/segment 安全校验与模板白名单校验。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_events import (
    FanOutExpandedEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepPlannedEvent,
)
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ResourceClaims,
    ResourcePath,
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
    RuntimeContext,
    TaskProjection,
)
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


class PlanError(AaError):
    """Plan 阶段 fail closed：结构危险、输入损坏或 reducer 契约被破坏。"""


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
    """node 当前代的冻结结局；只有 succeeded/skipped 对下游是「已解决」。"""

    status: Literal["succeeded", "skipped", "unresolved"]
    task: TaskProjection | None = None


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
            f"{projection.graph_digest} != compiled digest {compiled.digest}"
        )
    if projection.contract_digests != compiled.contract_digests:
        raise PlanError("graph_definition_changed: contract digests drifted from compiled pinning")
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
        # integrity/runtime FAIL：最高优先级；不完整的 wave 保持 pending。
        return result(terminal="fail", reason=fail_reason)

    scope, source_reads = _build_scope(graph, projection, artifacts, outcomes)
    delivery = _deliver_tokens(compiled, graph, projection, scope, outcomes)

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

    events: list[BaseModel] = []
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
        events,
        ready,
    )

    ready.extend(retry_tasks)
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
        raise PlanError(
            "nested subgraph invocations are planned by the child runtime (Task 12); "
            f"invocation {projection.invocation_id} has parent {projection.parent_invocation_id}"
        )
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
        if task.gate_report is not None:
            payload["gate"] = task.gate_report
            if "value" in task.gate_report:
                payload["value"] = task.gate_report["value"]
        return payload

    return Scope(variables, node_result=node_result), reads


# ---------------------------------------------------------------------------
# outcome seeding 与 token 传递


def _seed_outcomes(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
) -> tuple[dict[str, _Outcome], list[ExecutableTask], str | None]:
    """把投影中的 task 结局折叠成各 node 当前代 outcome；同时收集 retry 重计划。

    返回 ``(outcomes, retry_tasks, fail_reason)``。非重试失败或 retry/abandon
    耗尽的 task 产生 fail_reason（integrity/runtime FAIL，终局最高优先级）。
    """
    tasks_by_node: dict[str, list[TaskProjection]] = {}
    for task in projection.tasks.values():
        tasks_by_node.setdefault(task.node_id, []).append(task)

    outcomes: dict[str, _Outcome] = {}
    retry: list[ExecutableTask] = []
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
        if latest.status == "succeeded":
            outcomes[nid] = _Outcome(status="succeeded", task=latest)
            continue
        if latest.status in ("running", "pending", "interrupted"):
            # wave 仍在飞行：交由 lease/scheduler 对账，planner 不重复执行。
            outcomes[nid] = _Outcome(status="unresolved", task=latest)
            continue
        policy = _retry_policy(compiled, definition)
        if latest.status == "failed":
            retryable = (
                latest.error_kind is not None
                and latest.error_kind in policy.retry_on
                and latest.attempts_used < policy.max_attempts
            )
            if not retryable:
                return outcomes, retry, (
                    f"task {latest.task_id} (node '{nid}') failed with "
                    f"{latest.error_kind}; attempts {latest.attempts_used}/"
                    f"{policy.max_attempts}"
                )
        elif latest.attempts_used >= policy.max_attempts:  # abandoned
            return outcomes, retry, (
                f"task {latest.task_id} (node '{nid}') abandoned; retry budget "
                f"exhausted ({latest.attempts_used}/{policy.max_attempts})"
            )
        # failed-retryable 或 abandoned 且预算未耗尽：同一 task_id 进入下一 wave。
        outcomes[nid] = _Outcome(status="unresolved", task=latest)
        retry.append(_build_task(compiled, graph, projection, context, nid, len(node_tasks) - 1))
    return outcomes, retry, None


def _deliver_tokens(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    scope: Scope,
    outcomes: dict[str, _Outcome],
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

    # START 恒已解决：其出边在首次 Plan 即求值（invocation 输入）。
    for nid in graph.declaration_order:
        for edge in graph.nodes[nid].incoming:
            if edge.from_ == "START" and (edge.when is None or _satisfied(edge.when, scope, nid)):
                deliver(edge.to, "edge:START")
    for nid in graph.declaration_order:
        if outcomes[nid].status != "succeeded":
            continue
        cnode = graph.nodes[nid]
        targets: list[tuple[str, str]] = []
        for edge in cnode.outgoing:
            if edge.when is not None and not _satisfied(edge.when, scope, nid):
                continue
            targets.append((edge.to, f"edge:{nid}"))
        for route in cnode.routes:
            try:
                target, descriptor = _resolve_route(route, scope, nid)
            except _StopResolution as exc:
                if stop_reason is None:
                    stop_reason = str(exc)
                continue
            targets.append((target, descriptor))
        for target, descriptor in targets:
            deliver(target, descriptor)
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
    events: list[BaseModel],
    ready: list[ExecutableTask],
) -> None:
    """按拓扑序对每个尚无当前代结局的 node 做恰好一次激活/跳过决策。"""
    for nid in sorted(graph.nodes, key=lambda n: graph.nodes[n].topology_rank):
        cnode = graph.nodes[nid]
        definition = cnode.definition
        if outcomes[nid].status != "unresolved" or outcomes[nid].task is not None:
            continue  # 已有当前代结局，或 task 在飞行/重试中：不重放决策
        if definition.join is not None:
            _decide_join(
                compiled, graph, projection, context, scope, source_reads,
                outcomes, events, ready, nid,
            )
            continue

        tokens = delivery.selected.get(nid, [])
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
            outcomes[nid] = _Outcome(status="skipped")
            continue
        if definition.fan_out is not None:
            _decide_fan_out(
                compiled, graph, projection, context, scope, source_reads,
                outcomes, events, ready, nid,
            )
            continue
        if definition.when is not None and not _satisfied(definition.when, scope, nid):
            _emit_skip(graph, projection, source_reads, events, nid, definition.when)
            outcomes[nid] = _Outcome(status="skipped")
            continue
        ready.append(
            _activate(compiled, graph, projection, context, source_reads, events, nid, tokens)
        )
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
            raise PlanError(
                f"graph '{graph.graph_id}' join '{nid}' mode all_active has no "
                "activated source (all sources skipped); this is a runtime error, "
                "not an implicit pass"
            )
        # all：每个 source 已 succeeded 或 skipped；all_active：≥1 个已激活 source 成功。
    if definition.when is not None and not _satisfied(definition.when, scope, nid):
        _emit_skip(graph, projection, source_reads, events, nid, definition.when)
        outcomes[nid] = _Outcome(status="skipped")
        return
    descriptor = f"join:{join.mode}:{','.join(join.sources)}"
    ready.append(
        _activate(compiled, graph, projection, context, source_reads, events, nid, [descriptor])
    )


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
    frozen_ids = set(expansion.task_ids)
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
                return _Outcome(status="unresolved"), [], (
                    f"fan-out child {child.task_id} (node '{nid}') failed with "
                    f"{child.error_kind}; attempts {child.attempts_used}/{policy.max_attempts}"
                )
        elif child.attempts_used >= policy.max_attempts:  # abandoned
            return _Outcome(status="unresolved"), [], (
                f"fan-out child {child.task_id} (node '{nid}') abandoned; retry "
                f"budget exhausted ({child.attempts_used}/{policy.max_attempts})"
            )
        # failed-retryable 或 abandoned 且预算未耗尽：同一 task_id 进入下一 wave。
        retry.append(_build_fan_out_task(compiled, graph, projection, context, nid, expansion, index))

    settled = missing == 0 and waiting is None and not retry
    if settled and fan_out.reduce is not None:
        # 全部成功：reducer 契约与 child value 类型在这里 fail-closed 校验。
        _reduce_fan_out(compiled, graph, projection, nid, fan_out.reduce, expansion)
    if settled:
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
        raise PlanError(
            f"node '{nid}' fan_out items must resolve to a list, got {type(value).__name__}"
        )
    if len(value) > fan_out.max_items:
        raise PlanError(
            f"node '{nid}' fan_out expanded {len(value)} items, more than "
            f"max_items {fan_out.max_items}"
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
            raise PlanError(
                f"node '{nid}' fan_out duplicate key {canonical_key}; keys must be unique"
            )
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
    """解析 ``${<item_as>}`` 与 ``${context.change_id}`` 模板；其它变量一律拒绝。

    整串恰好一个模板时返回原值（标量/结构化 item 均可）；复合串把各模板替换为
    display 字符串。``path=True`` 时每个替换值必须是安全 path segment。
    """
    matches = list(_TEMPLATE.finditer(template))
    for match in matches:
        var = match.group(1)
        if var not in (item_as, "context.change_id"):
            raise PlanError(
                f"node '{nid}' template '${{{var}}}' is not '${{{item_as}}}' "
                "or '${context.change_id}'"
            )
    if not matches:
        return template

    def value_of(var: str) -> object:
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
        raise PlanError(
            f"node '{nid}' fan_out expansion has unsafe path segment {display!r}"
        )


def _assert_safe_path(path: str, nid: str) -> None:
    if not path.startswith(("change:", "project:", "repo:")):
        raise PlanError(
            f"node '{nid}' fan_out expansion path '{path}' must stay rooted "
            "in change:/project:/repo:"
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
    expanded: dict[str, list[str]] = {"reads": [], "writes": [], "exclusive": []}
    for field in ("reads", "writes"):
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
        raise PlanError(
            f"graph '{graph.graph_id}' node '{nid}' consumes unknown budget '{budget.consume}'"
        )
    limit = budget_def.limit
    if isinstance(limit, bool):
        raise PlanError(f"graph '{graph.graph_id}' budget '{budget.consume}' limit must be a non-negative int")
    if isinstance(limit, int):
        if limit < 0:
            raise PlanError(
                f"graph '{graph.graph_id}' budget '{budget.consume}' limit must be non-negative"
            )
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
                    raise PlanError(
                        f"graph '{graph.graph_id}' budget exhausted_to cycle at node '{current}'"
                    )
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
            activation_id=canonical_digest(
                {
                    "invocation_id": projection.invocation_id,
                    "checkpoint_ns": projection.checkpoint_ns,
                    "graph_id": graph.graph_id,
                    "node_id": nid,
                    "ordinal": ordinal,
                }
            ),
            input_sha256=canonical_digest(
                {"tokens": sorted(tokens), "task_input_sha256": task.input_sha256}
            ),
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


def _build_task(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    ordinal: int,
) -> ExecutableTask:
    definition = graph.nodes[nid].definition
    input_payload = {
        "with": dict(definition.with_),
        "context": {"change_id": context.change_id},
    }
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
        resources=_task_resources(compiled, graph, definition.uses),
        task_key=None,
        budget=_budget_mark(definition, projection, task_id),
    )


def _build_fan_out_task(
    compiled: CompiledWorkflow,
    graph: CompiledGraph,
    projection: GraphProjection,
    context: RuntimeContext,
    nid: str,
    expansion: FanOutExpansion,
    index: int,
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
            f"node '{nid}' fan_out child {index} task ID drifted from the frozen "
            "expansion (ledger integrity)"
        )
    input_payload: dict[str, object] = {
        "with": _expand_templates(dict(definition.with_), fan_out.item_as, item, context, nid),
        "context": {"change_id": context.change_id},
        "outputs": [
            _expand_output(output, fan_out.item_as, item, context, nid)
            for output in definition.outputs
        ],
        "fan_out": {"item_as": fan_out.item_as, "item": item, "task_key": display_key},
    }
    if definition.resources is not None:
        input_payload["resources"] = _expand_resources(
            definition.resources, fan_out.item_as, item, context, nid
        )
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
        resources=_task_resources(compiled, graph, definition.uses),
        task_key=display_key,
        budget=_budget_mark(definition, projection, task_id),
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
    chosen: str | None = None
    if label is not MISSING and isinstance(label, str):
        chosen = route.cases.get(label)
    if chosen is None:
        if route.default is not None:
            return route.default, f"route:{src}:default"
        raise _StopResolution(
            f"route from '{src}' select resolved to no declared case and has no "
            "default; failing closed to STOP"
        )
    return chosen, f"route:{src}:{label}"


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
    uses: str,
) -> ResourceClaims:
    """task 的资源 claim：subgraph 目标用子图 footprint，否则用本图保守并集。

    编译期 footprint 是全图 node claim 的保守并集（catalog 缺失时为
    ``global:exclusive``）；逐 node 收窄随 scheduler/handler 侧的 contract
    注入演进，保守方向只会增加串行，绝不放行冲突。
    """
    prefix, _, target = uses.partition(":")
    if prefix == "graph" and target in compiled.graphs:
        return compiled.graphs[target].resource_footprint
    return graph.resource_footprint


def _has_inflight(outcomes: dict[str, _Outcome]) -> bool:
    return any(
        outcome.status == "unresolved"
        and outcome.task is not None
        and outcome.task.status in ("running", "pending", "interrupted")
        for outcome in outcomes.values()
    )


__all__ = [
    "PlanError",
    "apply_state_updates",
    "fan_out_state_updates",
    "plan_superstep",
]
