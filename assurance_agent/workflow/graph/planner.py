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
- 同一决策点上有多条非互斥 token 同时选中同一普通 node 时 fail closed
  （PlanError，提示改用 builtin:join），绝不重复激活。设计 §6.5 把这一拒绝
  划归 compiler；在 compiler 获得该静态检查之前，planner 以运行时守卫兜底。
- 终局优先级：integrity/runtime FAIL > STOP/REJECT > pending interrupt >
  retry pending > completed；出现高优先级结果时不再 Plan 新 task。

当前边界（后续 task 扩展，均在 planner.py 内演进）：fan-out 展开属于 Task 7
（选中 fan-out node 时显式 PlanError）；business budget 消耗检查同属 Task 7；
cyclic SCC 的跨代再激活（activation ordinal > 0 的新 token 代）随 Task 7/11
落地，本实现完整支持 DAG 与单代语义；嵌套 subgraph invocation 的 graph 解析
属于 Task 12，这里只接受 root invocation（经 entrypoint 解析 graph）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_events import (
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepPlannedEvent,
)
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import ResourceClaims
from assurance_agent.workflow.graph.models import (
    ArtifactReader,
    CompiledGraph,
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.schema_v2 import (
    NodeDef,
    RetryPolicyDef,
    RouteDef,
    StateDef,
    TimeoutPolicyDef,
)
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    DslError,
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
    delivery = _deliver_tokens(graph, scope, outcomes)

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
    ready.sort(key=lambda task: _task_sort_key(graph, task))
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
            return {}
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
            # fan-out 的冻结展开/聚合属于 Task 7；本代只保证不误判为完成。
            outcomes[nid] = _Outcome(status="unresolved", task=node_tasks[0] if node_tasks else None)
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
    graph: CompiledGraph,
    scope: Scope,
    outcomes: dict[str, _Outcome],
) -> _Delivery:
    """求值所有已成功 node 的出边与 route，冻结选中 token 与终局标记。

    被 skip 的 node 不遍历出边。route 的缺失/MISSING select 无 default 时
    fail closed 到 STOP；edge/route 显式选中 FAIL 是 runtime FAIL 终局。
    join node 的 token 由其声明的 join.sources 表达，镜像 edge/route 不投递。
    """
    selected: dict[str, list[str]] = {}
    end_reached = False
    stop_reason: str | None = None
    fail_reason: str | None = None

    def deliver(target: str, descriptor: str) -> None:
        nonlocal end_reached, stop_reason, fail_reason
        if target == "END":
            end_reached = True
        elif target == "STOP":
            if stop_reason is None:
                stop_reason = f"selected STOP via {descriptor}"
        elif target == "FAIL":
            if fail_reason is None:
                fail_reason = f"selected FAIL via {descriptor}"
        elif target in graph.nodes and graph.nodes[target].definition.join is None:
            selected.setdefault(target, []).append(descriptor)

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
            _emit_skip(graph, projection, source_reads, events, nid, "(no incoming edge selected)")
            outcomes[nid] = _Outcome(status="skipped")
            continue
        if definition.fan_out is not None:
            raise PlanError(
                f"graph '{graph.graph_id}' node '{nid}' declares fan_out; "
                "frozen expansion is implemented in Task 7"
            )
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
    return ExecutableTask(
        task_id=_task_id(projection, graph.graph_id, nid, ordinal, None),
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


def _task_sort_key(graph: CompiledGraph, task: ExecutableTask) -> tuple[int, int, str]:
    cnode = graph.nodes[task.node_id]
    return (cnode.topology_rank, cnode.declaration_index, task.task_id)


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
    """前驱结果是否全部稳定：edge/route 源 succeeded 或 skipped；START 恒已解决。"""
    for edge in graph.nodes[nid].incoming:
        if edge.from_ != "START" and outcomes[edge.from_].status == "unresolved":
            return False
    for other in graph.nodes.values():
        if outcomes[other.node_id].status == "unresolved" and any(
            nid in route.cases.values() or nid == route.default for route in other.routes
        ):
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
    "plan_superstep",
]
