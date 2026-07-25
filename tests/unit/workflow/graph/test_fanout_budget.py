"""冻结 fan-out 展开与 ledger 权威业务预算。

覆盖：``fan_out_expanded`` 事件一次性冻结 items、keys、source hashes 与 child ID；
child 按源列表顺序（而非 task ID 哈希序）返回；source drift fail closed 到
``fan_out_source_drift``；resume 只重放冻结展开、不发第二个展开事件；重复 key、
超过 ``max_items``、非 JSON item、不安全路径 key 与 reducer 目标/类型不匹配全部
fail closed；``completion: all`` 等待每个冻结 child，reduce 按冻结 item 序而非
完成序合并；budget 耗尽在任何新 task 创建前改道 ``exhausted_to``；
``consumption_id`` 按 task（而非 attempt）幂等派生；失败/abandon 消耗零个单位；
多次技术重试后的唯一成功恰好消耗一个单位。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.core.graph_events import (
    FanOutExpandedEvent,
    NodeActivatedEvent,
    SuperstepPlannedEvent,
)
from assurance_agent.workflow.graph.checkpoint import project_invocation
from assurance_agent.workflow.graph.compiler import canonical_digest, compile_workflow
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    FanOutExpansion,
    GraphProjection,
    NodeGeneration,
    NodeHistory,
    ResolvedArtifact,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.node_history import fan_out_aggregate_task_id, node_history_key
from assurance_agent.workflow.graph.planner import (
    PlanError,
    _build_fan_out_aggregate_task,
    fan_out_state_updates,
    plan_superstep,
)
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2

FANOUT_GRAPH = """
schema_version: "2"
name: planner-fanout
entrypoints:
  full: {graph: main}
policies:
  retry:
    transient: {max_attempts: 3, retry_on: [internal]}
graphs:
  main:
    max_supersteps: 8
    state:
      generated_cases: {type: list, default: [], reducer: append}
    nodes:
      gen:
        uses: operation:gen-op
        outputs: [change:explore/advisory.json]
      per-module:
        uses: operation:case-op
        retry: transient
        fan_out:
          items: advisory.modules
          item_as: module
          key: "${module}"
          max_items: 4
          completion: all
          reduce: {into: generated_cases, using: append}
        with:
          module: "${module}"
        outputs: ["change:cases/${module}/case.yaml"]
      report: {uses: operation:report-op}
    edges:
      - {from: START, to: gen}
      - {from: gen, to: per-module}
      - {from: per-module, to: report}
      - {from: report, to: END}
"""

FANOUT_PARAMS_GRAPH = """
schema_version: "2"
name: planner-fanout-params
params:
  modules: {type: list, default: []}
entrypoints:
  full: {graph: main}
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
        with:
          module: "${module}"
        outputs: ["change:cases/${module}/case.yaml"]
    edges:
      - {from: START, to: per-module}
      - {from: per-module, to: END}
"""

FANOUT_REDUCE_TYPE_MISMATCH = """
schema_version: "2"
name: planner-fanout-reduce-mismatch
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    state:
      summary: {type: object, default: {}, reducer: merge_disjoint}
    nodes:
      per-module:
        uses: operation:case-op
        fan_out:
          items: params.modules
          item_as: module
          key: "${module}"
          max_items: 4
          reduce: {into: summary, using: append}
    edges:
      - {from: START, to: per-module}
      - {from: per-module, to: END}
params:
  modules: {type: list, default: []}
"""

BUDGET_GRAPH = """
schema_version: "2"
name: planner-budget
entrypoints:
  full: {graph: main}
policies:
  retry:
    transient: {max_attempts: 5, retry_on: [internal]}
graphs:
  main:
    max_supersteps: 16
    budgets:
      fix_attempts: {limit: 2}
    nodes:
      review: {uses: operation:review-op}
      fix:
        uses: operation:fix-op
        retry: transient
        budget: {consume: fix_attempts, "on": committed, exhausted_to: exhausted}
      exhausted: {uses: operation:exhausted-op}
    edges:
      - {from: START, to: review}
      - {from: fix, to: review}
      - {from: exhausted, to: END}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
          needs_fix: fix
        default: STOP
"""

BUDGET_PARAM_GRAPH = """
schema_version: "2"
name: planner-budget-param
params:
  max_fix: {type: int, default: 1}
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 16
    budgets:
      fix_attempts: {limit: params.max_fix}
    nodes:
      review: {uses: operation:review-op}
      fix:
        uses: operation:fix-op
        budget: {consume: fix_attempts, "on": committed, exhausted_to: exhausted}
      exhausted: {uses: operation:exhausted-op}
    edges:
      - {from: START, to: review}
      - {from: fix, to: review}
      - {from: exhausted, to: END}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
          needs_fix: fix
        default: STOP
"""


class _FakeArtifacts:
    """dict 支撑的 ArtifactReader：缺失路径抛 KeyError（按 MISSING fail-closed）。"""

    def __init__(self, payloads: dict[tuple[str, str], object] | None = None) -> None:
        self._payloads = payloads or {}

    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        payload = self._payloads[(tree_id, logical_path)]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return ResolvedArtifact(
            value=payload,
            reads_sha256={logical_path: hashlib.sha256(canonical.encode("utf-8")).hexdigest()},
        )


def _compile(text: str) -> CompiledWorkflow:
    return compile_workflow(parse_workflow_v2(text))


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
    params: dict[str, object] | None = None,
    tasks: list[TaskProjection] | None = None,
    state_values: dict[str, object] | None = None,
    current_tree_id: str = "tree-0",
    budgets: dict[str, int] | None = None,
    fan_out_expansions: dict[str, FanOutExpansion] | None = None,
    node_histories: dict[str, NodeHistory] | None = None,
) -> GraphProjection:
    return GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest=compiled.digest,
        contract_digests={},
        params=params or {},
        root_tree_id="tree-0",
        current_tree_id=current_tree_id,
        state_values=state_values or {},
        tasks={task.task_id: task for task in tasks or []},
        budgets=budgets or {},
        fan_out_expansions=fan_out_expansions or {},
        node_histories=node_histories or {},
    )


def _fan_out_committed_histories(
    projection: GraphProjection,
    node_id: str,
    expansion: FanOutExpansion,
    *,
    graph_id: str = "main",
) -> dict[str, NodeHistory]:
    aggregate_id = fan_out_aggregate_task_id(
        invocation_id=projection.invocation_id,
        checkpoint_ns=projection.checkpoint_ns,
        structural_path=projection.structural_path,
        graph_id=graph_id,
        node_id=node_id,
        child_count=len(expansion.task_ids),
    )
    key = node_history_key(projection.checkpoint_ns, graph_id, node_id)
    generation = NodeGeneration(
        generation_ordinal=0,
        status="succeeded",
        outputs_committed=True,
        frozen_outputs={},
        aggregate_task_id=aggregate_id,
    )
    return {key: NodeHistory(latest_generation_ordinal=0, generations_by_ordinal={0: generation})}


def _aggregate_task(
    compiled: CompiledWorkflow,
    tmp_path: Path,
    expansion: FanOutExpansion,
    *,
    node_id: str = "per-module",
) -> ExecutableTask:
    return _build_fan_out_aggregate_task(
        compiled,
        compiled.graphs["main"],
        _projection(compiled),
        _context(tmp_path),
        node_id,
        expansion,
    )


def _task(task: ExecutableTask, status: str, **overrides: object) -> TaskProjection:
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "node_id": task.node_id,
        "status": status,
        "attempts_used": 1,
        "latest_attempt_id": f"{task.task_id}-a1",
    }
    payload.update(overrides)
    return TaskProjection(**payload)  # type: ignore[arg-type]


def _plan(
    compiled: CompiledWorkflow,
    projection: GraphProjection,
    tmp_path: Path,
    artifacts: _FakeArtifacts | None = None,
):
    return plan_superstep(compiled, projection, _context(tmp_path), artifacts or _FakeArtifacts())


def _initial_tasks(compiled: CompiledWorkflow, tmp_path: Path) -> dict[str, ExecutableTask]:
    plan = _plan(compiled, _projection(compiled), tmp_path)
    return {task.node_id: task for task in plan.tasks}


def _fanout_projection(
    compiled: CompiledWorkflow,
    tmp_path: Path,
    modules: object,
) -> tuple[GraphProjection, _FakeArtifacts]:
    """gen 已成功、advisory 已提交到 tree-1 的投影；per-module 待展开。"""
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    advisory = {"modules": modules}
    artifacts = _FakeArtifacts({("tree-1", "change:explore/advisory.json"): advisory})
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[_task(gen, "succeeded")],
    )
    return projection, artifacts


def _expanded_event(
    compiled: CompiledWorkflow, tmp_path: Path
) -> tuple[FanOutExpandedEvent, list[ExecutableTask]]:
    projection, artifacts = _fanout_projection(compiled, tmp_path, ["menu", "order"])
    plan = _plan(compiled, projection, tmp_path, artifacts)
    event = next(e for e in plan.strict_events if isinstance(e, FanOutExpandedEvent))
    return event, list(plan.tasks)


def _frozen_expansion(event: FanOutExpandedEvent) -> FanOutExpansion:
    return FanOutExpansion(
        items=tuple(event.items),
        task_keys=tuple(event.task_keys),
        task_ids=tuple(event.task_ids),
        source_reads_sha256=dict(event.source_reads_sha256),
    )


# ---------------------------------------------------------------------------
# Step 1：fan-out 冻结展开


def test_fan_out_expansion_freezes_items_keys_hashes_and_child_ids(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    projection, artifacts = _fanout_projection(compiled, tmp_path, ["menu", "order"])
    plan = _plan(compiled, projection, tmp_path, artifacts)

    # 两个 child 按源列表顺序返回（不是 task ID 哈希序）。
    assert [task.node_id for task in plan.tasks] == ["per-module", "per-module"]
    assert [task.task_key for task in plan.tasks] == ["menu", "order"]
    payloads = [cast(dict[str, object], task.input) for task in plan.tasks]
    assert [cast(dict[str, object], p["with"])["module"] for p in payloads] == ["menu", "order"]
    assert [p["outputs"] for p in payloads] == [
        ["change:cases/menu/case.yaml"],
        ["change:cases/order/case.yaml"],
    ]

    # 恰好一个展开事件，冻结 items、keys、source hashes 与 child IDs，且先于 superstep_planned。
    expanded = [e for e in plan.strict_events if isinstance(e, FanOutExpandedEvent)]
    assert len(expanded) == 1
    event = expanded[0]
    assert event.graph_id == "main"
    assert event.node_id == "per-module"
    assert event.items == ["menu", "order"]
    assert event.task_keys == ["menu", "order"]
    assert event.task_ids == [task.task_id for task in plan.tasks]
    expected_sha = hashlib.sha256(b'{"modules":["menu","order"]}').hexdigest()
    assert event.source_reads_sha256 == {"change:explore/advisory.json": expected_sha}
    assert isinstance(plan.strict_events[-1], SuperstepPlannedEvent)

    # 确定性：同一投影重放逐字节一致。
    replay = _plan(compiled, projection, tmp_path, artifacts)
    assert [task.task_id for task in replay.tasks] == [task.task_id for task in plan.tasks]
    assert [e.model_dump_json() for e in replay.strict_events] == [
        e.model_dump_json() for e in plan.strict_events
    ]


def test_fan_out_source_drift_fails_closed(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    event, _ = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    resumed = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[_task(gen, "succeeded")],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )

    # advisory 在展开事件之后被改写：fail closed，绝不重算不同的 item 列表。
    drifted = _FakeArtifacts(
        {("tree-1", "change:explore/advisory.json"): {"modules": ["menu", "order", "extra"]}}
    )
    with pytest.raises(PlanError, match="fan_out_source_drift"):
        _plan(compiled, resumed, tmp_path, drifted)

    # source artifact 消失同样按 drift fail closed。
    with pytest.raises(PlanError, match="fan_out_source_drift"):
        _plan(compiled, resumed, tmp_path, _FakeArtifacts())


def test_fan_out_resume_replays_frozen_expansion_without_second_event(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    resumed = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[_task(gen, "succeeded")],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )
    _, artifacts = _fanout_projection(compiled, tmp_path, ["menu", "order"])
    plan = _plan(compiled, resumed, tmp_path, artifacts)

    # source 未变：child 按冻结数据重建为同一批 ID，不发第二个展开事件。
    assert [task.task_id for task in plan.tasks] == [task.task_id for task in children]
    assert not any(isinstance(e, FanOutExpandedEvent) for e in plan.strict_events)
    planned = [e for e in plan.strict_events if isinstance(e, SuperstepPlannedEvent)]
    assert len(planned) == 1
    assert planned[0].task_ids == [task.task_id for task in children]


def test_fan_out_duplicate_keys_fail_closed(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    projection, artifacts = _fanout_projection(compiled, tmp_path, ["menu", "menu"])
    with pytest.raises(PlanError, match="duplicate"):
        _plan(compiled, projection, tmp_path, artifacts)


def test_fan_out_more_than_max_items_fail_closed(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    projection, artifacts = _fanout_projection(compiled, tmp_path, ["a", "b", "c", "d", "e"])
    with pytest.raises(PlanError, match="max_items"):
        _plan(compiled, projection, tmp_path, artifacts)


def test_fan_out_non_json_items_fail_closed(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_PARAMS_GRAPH)
    projection = _projection(compiled, params={"modules": [{"bad": {"not", "json"}}]})
    with pytest.raises(PlanError, match="JSON"):
        _plan(compiled, projection, tmp_path)


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".", "..", "", "a\\b"])
def test_fan_out_unsafe_path_keys_fail_closed(tmp_path: Path, bad: str) -> None:
    compiled = _compile(FANOUT_GRAPH)
    projection, artifacts = _fanout_projection(compiled, tmp_path, ["menu", bad])
    with pytest.raises(PlanError, match="unsafe"):
        _plan(compiled, projection, tmp_path, artifacts)


def test_fan_out_reducer_type_mismatch_fail_closed(tmp_path: Path) -> None:
    # state key 是 object，reduce 却声明 append（list reducer）：目标/类型不匹配。
    compiled = _compile(FANOUT_REDUCE_TYPE_MISMATCH)
    projection = _projection(compiled, params={"modules": ["menu"]})
    with pytest.raises(PlanError, match="reducer"):
        _plan(compiled, projection, tmp_path)


def test_fan_out_unknown_template_fail_closed(tmp_path: Path) -> None:
    text = FANOUT_PARAMS_GRAPH.replace('key: "${module}"', 'key: "${other}"')
    compiled = _compile(text)
    projection = _projection(compiled, params={"modules": ["menu"]})
    with pytest.raises(PlanError, match="template"):
        _plan(compiled, projection, tmp_path)


def test_fan_out_fold_projects_frozen_expansion(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append(
        change,
        [
            _started(),
            {
                "source": "graph",
                "type": "node_activated",
                "invocation_id": "inv-1",
                "checkpoint_ns": "inv-1",
                "graph_id": "main",
                "node_id": "per-module",
                "generation_ordinal": 0,
                "activation_id": "act-fo-0",
                "input_sha256": "in-fo",
                "source_reads_sha256": {"change:explore/advisory.json": "sha-1"},
            },
            {
                "source": "graph",
                "type": "fan_out_expanded",
                "invocation_id": "inv-1",
                "checkpoint_ns": "inv-1",
                "graph_id": "main",
                "node_id": "per-module",
                "generation_ordinal": 0,
                "source_reads_sha256": {"change:explore/advisory.json": "sha-1"},
                "items": ["menu", "order"],
                "task_keys": ["menu", "order"],
                "task_ids": ["tid-menu", "tid-order"],
            },
        ],
    )
    projection = project_invocation(change, "inv-1")
    expansion = projection.fan_out_expansions["per-module"]
    assert expansion.items == ("menu", "order")
    assert expansion.task_keys == ("menu", "order")
    assert expansion.task_ids == ("tid-menu", "tid-order")
    assert expansion.source_reads_sha256 == {"change:explore/advisory.json": "sha-1"}


# ---------------------------------------------------------------------------
# Step 5：确定性 fan-out reduce（completion: all）


def _succeeded_children(
    compiled: CompiledWorkflow, tmp_path: Path, values: dict[str, object]
) -> tuple[FanOutExpansion, list[TaskProjection]]:
    """按完成序（values dict 序）构造全部成功的 child 投影，用于证明合并序与完成序无关。"""
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    tasks = [_task(gen, "succeeded")]
    by_key = {task.task_key: task for task in children}
    for key, value in values.items():
        tasks.append(_task(by_key[key], "succeeded", value=value))
    return _frozen_expansion(event), tasks


def test_fan_out_reduce_waits_for_all_children_and_uses_frozen_item_order(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    # 完成序：order 先成功、menu 后成功；合并必须仍按冻结 item 序（menu 在前）。
    expansion, tasks = _succeeded_children(compiled, tmp_path, {"order": ["o1"], "menu": ["m1", "m2"]})
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=tasks,
        fan_out_expansions={"per-module": expansion},
    )
    plan = _plan(compiled, projection, tmp_path)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].node_id == "per-module"
    assert plan.tasks[0].task_key == "__aggregate__"

    updates = fan_out_state_updates(compiled, projection)
    assert updates == [("reduce:main:per-module", {"generated_cases": ["m1", "m2", "o1"]})]

    aggregate = _aggregate_task(compiled, tmp_path, expansion)
    committed = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[*tasks, _task(aggregate, "succeeded", fan_out_aggregate=True, task_key="__aggregate__")],
        fan_out_expansions={"per-module": expansion},
        node_histories=_fan_out_committed_histories(_projection(compiled), "per-module", expansion),
    )
    followup = _plan(compiled, committed, tmp_path)
    assert [task.node_id for task in followup.tasks] == ["report"]


def test_fan_out_reduce_stays_pending_while_child_in_flight(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    by_key = {task.task_key: task for task in children}
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[
            _task(gen, "succeeded"),
            _task(by_key["menu"], "succeeded", value=["m1"]),
            _task(by_key["order"], "running"),
        ],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )
    plan = _plan(compiled, projection, tmp_path)
    # 任一 child 未 settle：不计划下游，也不发布部分聚合。
    assert plan.tasks == ()
    assert plan.terminal is None
    assert fan_out_state_updates(compiled, projection) == []


def test_fan_out_failed_child_is_retried_with_same_task_id(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    by_key = {task.task_key: task for task in children}
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[
            _task(gen, "succeeded"),
            _task(by_key["menu"], "succeeded", value=["m1"]),
            _task(by_key["order"], "failed", error_kind="internal", attempts_used=1),
        ],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )
    plan = _plan(compiled, projection, tmp_path)
    # 失败 child 以同一 task ID 重计划；成功 child 的写入保持 pending，不重新执行。
    assert [task.task_id for task in plan.tasks] == [by_key["order"].task_id]
    assert fan_out_state_updates(compiled, projection) == []


def test_fan_out_retry_child_carries_prior_failure_feedback(tmp_path: Path) -> None:
    text = FANOUT_GRAPH.replace("retry_on: [internal]", "retry_on: [invalid_output]", 1)
    compiled = _compile(text)
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    by_key = {task.task_key: task for task in children}
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[
            _task(gen, "succeeded"),
            _task(by_key["menu"], "succeeded", value=["m1"]),
            _task(
                by_key["order"],
                "failed",
                error_kind="invalid_output",
                error="output 'change:out/order.json' failed schema validation",
                attempts_used=1,
            ),
        ],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )
    plan = _plan(compiled, projection, tmp_path)
    assert len(plan.tasks) == 1
    retry_task = plan.tasks[0]
    assert retry_task.task_id == by_key["order"].task_id
    assert retry_task.prior_error_kind == "invalid_output"
    assert retry_task.prior_failure is not None
    assert "schema validation" in retry_task.prior_failure


def test_fan_out_non_retryable_child_failure_is_terminal_fail(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    event, children = _expanded_event(compiled, tmp_path)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    by_key = {task.task_key: task for task in children}
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[
            _task(gen, "succeeded"),
            _task(by_key["menu"], "succeeded", value=["m1"]),
            _task(by_key["order"], "failed", error_kind="auth", attempts_used=1),
        ],
        fan_out_expansions={"per-module": _frozen_expansion(event)},
    )
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == "fail"
    assert plan.tasks == ()
    assert fan_out_state_updates(compiled, projection) == []


def test_fan_out_reduce_value_type_mismatch_fails_closed(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    expansion, tasks = _succeeded_children(compiled, tmp_path, {"menu": ["m1"], "order": "not-a-list"})
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=tasks,
        fan_out_expansions={"per-module": expansion},
    )
    with pytest.raises(PlanError, match="append"):
        _plan(compiled, projection, tmp_path)


def test_fan_out_empty_items_resolves_without_children(tmp_path: Path) -> None:
    compiled = _compile(FANOUT_GRAPH)
    projection, artifacts = _fanout_projection(compiled, tmp_path, [])
    plan = _plan(compiled, projection, tmp_path, artifacts)
    assert plan.tasks == ()
    assert plan.terminal is None
    expanded = [e for e in plan.strict_events if isinstance(e, FanOutExpandedEvent)]
    assert len(expanded) == 1
    assert expanded[0].items == []
    assert expanded[0].task_ids == []

    # 展开冻结后下一代：node 无 child 即解决，reduce 落在 state default 上。
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    resumed = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[_task(gen, "succeeded")],
        fan_out_expansions={"per-module": _frozen_expansion(expanded[0])},
    )
    followup = _plan(compiled, resumed, tmp_path, artifacts)
    assert len(followup.tasks) == 1
    assert followup.tasks[0].node_id == "per-module"
    assert followup.tasks[0].task_key == "__aggregate__"
    assert fan_out_state_updates(compiled, resumed) == [("reduce:main:per-module", {"generated_cases": []})]

    expansion = _frozen_expansion(expanded[0])
    aggregate = _aggregate_task(compiled, tmp_path, expansion)
    committed = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[
            _task(gen, "succeeded"),
            _task(aggregate, "succeeded", fan_out_aggregate=True, task_key="__aggregate__"),
        ],
        fan_out_expansions={"per-module": expansion},
        node_histories=_fan_out_committed_histories(_projection(compiled), "per-module", expansion),
    )
    report_plan = _plan(compiled, committed, tmp_path, artifacts)
    assert [task.node_id for task in report_plan.tasks] == ["report"]


# ---------------------------------------------------------------------------
# Step 3：业务预算


def test_budget_consumer_is_marked_with_derived_consumption_id(tmp_path: Path) -> None:
    compiled = _compile(BUDGET_GRAPH)
    review = _initial_tasks(compiled, tmp_path)["review"]
    projection = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["fix"]
    fix = plan.tasks[0]
    assert fix.budget is not None
    assert fix.budget.budget_id == "fix_attempts"
    assert fix.budget.consumption_id == canonical_digest(
        {
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "budget_id": "fix_attempts",
            "task_id": fix.task_id,
        }
    )

    # 同一投影重放：consumption_id 幂等（同一 task 的重复 staging 是同一事件，而非新单位）。
    replay = _plan(compiled, projection, tmp_path)
    assert replay.tasks[0].budget is not None
    assert replay.tasks[0].budget.consumption_id == fix.budget.consumption_id


def test_budget_exhaustion_reroutes_to_exhausted_to_without_task(tmp_path: Path) -> None:
    compiled = _compile(BUDGET_GRAPH)
    review = _initial_tasks(compiled, tmp_path)["review"]
    # limit 2 已投影两个不同的 consumption：第三次选中 consumer 改道 exhausted_to。
    projection = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
        budgets={"fix_attempts": 2},
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["exhausted"]
    assert all(task.node_id != "fix" for task in plan.tasks)
    activated = [e for e in plan.strict_events if isinstance(e, NodeActivatedEvent)]
    assert [e.node_id for e in activated] == ["exhausted"]


def test_budget_limit_from_params(tmp_path: Path) -> None:
    compiled = _compile(BUDGET_PARAM_GRAPH)
    review = _initial_tasks(compiled, tmp_path)["review"]
    needs_fix = _projection(
        compiled,
        params={"max_fix": 0},
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
    )
    plan = _plan(compiled, needs_fix, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["exhausted"]

    headroom = _projection(
        compiled,
        params={"max_fix": 3},
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
        budgets={"fix_attempts": 2},
    )
    assert [task.node_id for task in _plan(compiled, headroom, tmp_path).tasks] == ["fix"]


def test_retry_replan_keeps_single_consumption_id(tmp_path: Path) -> None:
    compiled = _compile(BUDGET_GRAPH)
    review = _initial_tasks(compiled, tmp_path)["review"]
    needs_fix = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
    )
    fix = _plan(compiled, needs_fix, tmp_path).tasks[0]
    assert fix.budget is not None

    # 技术失败后的重试重计划同一 task_id：consumption_id 不变，成功时只消耗一个单位。
    failed = _projection(
        compiled,
        tasks=[
            _task(review, "succeeded", gate_report={"verdict": "needs_fix"}),
            _task(fix, "failed", error_kind="internal", attempts_used=1),
        ],
    )
    replanned = _plan(compiled, failed, tmp_path).tasks[0]
    assert replanned.task_id == fix.task_id
    assert replanned.budget is not None
    assert replanned.budget.consumption_id == fix.budget.consumption_id


# ---------------------------------------------------------------------------
# ledger 权威计数（fold + planner 联合）


def _append(change: Path, events: list[dict]) -> None:
    for event in events:
        append_event_strict(change, event)


def _started(graph_digest: str = "gd-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": "inv-1",
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": graph_digest,
        "contract_digests": {},
        "params": {},
        "params_sha256": "ps-1",
        "root_tree_id": "tree-0",
        "max_parallel_tasks": 2,
        "checkpoint_ns": "inv-1",
        "structural_path": "main",
    }


def _planned(task_ids: list[str]) -> dict:
    return {
        "source": "graph",
        "type": "superstep_planned",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "superstep_id": "ss-1",
        "checkpoint_id": "cp-0",
        "task_ids": task_ids,
    }


def _begin(task_id: str, node_id: str, attempt: int) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_started",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "node_id": node_id,
        "input_sha256": "in-1",
        "graph_digest": "gd-1",
        "contract_digest": "cd-1",
        "attempt_number": attempt,
        "lease_expires_at": "2026-07-19T00:00:00+00:00",
        "started_at": "2026-07-19T00:00:00+00:00",
    }


def _succeeded(task_id: str, attempt: int, value: object = None) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_succeeded",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "write_set_id": f"ws-{task_id}",
        "outputs_sha256": {},
        "gate_report": None,
        "state_updates": {},
        "value": value,
    }


def _failed(task_id: str, attempt: int) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_failed",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "error_kind": "internal",
        "message": "boom",
        "next_retry_at": "2026-07-19T00:01:00+00:00",
    }


def _abandoned(task_id: str, attempt: int) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_abandoned",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "reason": "lease expired",
        "abandoned_at": "2026-07-19T00:02:00+00:00",
    }


def _budget(budget_id: str, consumption_id: str, task_id: str) -> dict:
    return {
        "source": "graph",
        "type": "budget_consumed",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "graph_id": "main",
        "budget_id": budget_id,
        "consumption_id": consumption_id,
        "task_id": task_id,
    }


def test_failed_or_abandoned_task_consumes_zero_budget(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append(
        change,
        [
            _started(),
            _planned(["task-fix"]),
            _begin("task-fix", "fix", attempt=1),
            _failed("task-fix", attempt=1),
            _begin("task-fix", "fix", attempt=2),
            _abandoned("task-fix", attempt=2),
        ],
    )
    projection = project_invocation(change, "inv-1")
    # 失败与 abandon 都不产生 budget_consumed：消耗零个单位。
    assert projection.budgets == {}
    assert projection.tasks["task-fix"].attempts_used == 2
    assert projection.tasks["task-fix"].status == "abandoned"


def test_retries_then_single_success_consumes_exactly_one_unit(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    consumption_id = canonical_digest(
        {
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "budget_id": "fix_attempts",
            "task_id": "task-fix",
        }
    )
    _append(
        change,
        [
            _started(),
            _planned(["task-fix"]),
            _begin("task-fix", "fix", attempt=1),
            _failed("task-fix", attempt=1),
            _begin("task-fix", "fix", attempt=2),
            _failed("task-fix", attempt=2),
            _begin("task-fix", "fix", attempt=3),
            _failed("task-fix", attempt=3),
            _begin("task-fix", "fix", attempt=4),
            _succeeded("task-fix", attempt=4),
            _budget("fix_attempts", consumption_id, "task-fix"),
        ],
    )
    projection = project_invocation(change, "inv-1")
    # 三次技术重试不消耗；唯一成功恰好消耗一个单位。
    assert projection.budgets == {"fix_attempts": 1}
    assert projection.tasks["task-fix"].attempts_used == 4
    assert projection.tasks["task-fix"].status == "succeeded"


def test_budget_projection_counts_unique_consumptions(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append(
        change,
        [
            _started(),
            _planned(["task-a", "task-b"]),
            _begin("task-a", "fix", attempt=1),
            _begin("task-b", "fix", attempt=1),
            _succeeded("task-a", attempt=1),
            _succeeded("task-b", attempt=1),
            _budget("fix_attempts", "c-a", "task-a"),
            _budget("fix_attempts", "c-b", "task-b"),
        ],
    )
    projection = project_invocation(change, "inv-1")
    # 只按唯一 (budget_id, consumption_id) 计数；重复拒绝由 test_checkpoint 覆盖。
    assert projection.budgets == {"fix_attempts": 2}
