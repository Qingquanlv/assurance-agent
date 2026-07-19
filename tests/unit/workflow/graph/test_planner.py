"""纯 Plan 语义：激活、同 step 不可见性、route 冻结、join、reducer 与终局优先级。

覆盖：diamond 初始 Plan 的声明序与稳定 ID；单 success 不解锁下游；双 success
解锁 join；互斥 START 入边只激活一次；非互斥汇入被拒绝而非重复激活；三种
join 模式；route default 与 fail-closed STOP；sibling pending state 不可见；
artifact scope 与 source_reads 冻结；retry/abandon 重计划；终局优先级；
max_supersteps 安全阀；四种 typed reducer 的确定性语义。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.core.graph_events import (
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepPlannedEvent,
)
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    InterruptProjection,
    ResolvedArtifact,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import (
    PlanError,
    apply_state_updates,
    plan_superstep,
)
from assurance_agent.workflow.graph.schema_v2 import StateDef, parse_workflow_v2

DIAMOND = """
schema_version: "2"
name: planner-diamond
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      left: {uses: operation:left-op}
      right: {uses: operation:right-op}
      join:
        uses: builtin:join
        join: {sources: [left, right], mode: all}
    edges:
      - {from: START, to: left}
      - {from: START, to: right}
      - {from: left, to: join}
      - {from: right, to: join}
      - {from: join, to: END}
"""

EXCLUSIVE_START = """
schema_version: "2"
name: planner-exclusive
params:
  mode: {type: str, default: "a"}
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 4
    nodes:
      pick: {uses: operation:pick-op}
    edges:
      - {from: START, to: pick, when: "params.mode == 'a'"}
      - {from: START, to: pick, when: "params.mode != 'a'"}
      - {from: pick, to: END}
"""

NON_EXCLUSIVE = """
schema_version: "2"
name: planner-non-exclusive
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      left: {uses: operation:left-op}
      right: {uses: operation:right-op}
      gather: {uses: operation:gather-op}
    edges:
      - {from: START, to: left}
      - {from: START, to: right}
      - {from: left, to: gather}
      - {from: right, to: gather}
      - {from: gather, to: END}
"""

JOIN_GRAPH = """
schema_version: "2"
name: planner-join
params:
  go: {type: bool, default: true}
  flag: {type: bool, default: true}
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      a: {uses: operation:a-op, when: "params.go == true"}
      b: {uses: operation:b-op, when: "params.flag == true"}
      j:
        uses: builtin:join
        join: {sources: [a, b], mode: __MODE__}
    edges:
      - {from: START, to: a}
      - {from: START, to: b}
      - {from: a, to: j}
      - {from: b, to: j}
      - {from: j, to: END}
"""

ROUTE_GRAPH = """
schema_version: "2"
name: planner-route
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      review: {uses: operation:review-op}
      fix: {uses: operation:fix-op}
    edges:
      - {from: START, to: review}
      - {from: fix, to: END}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
          needs_fix: fix
        default: STOP
"""

ROUTE_NO_DEFAULT = """
schema_version: "2"
name: planner-route-no-default
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      review: {uses: operation:review-op}
    edges:
      - {from: START, to: review}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
"""

STATE_GRAPH = """
schema_version: "2"
name: planner-state
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    state:
      x: {type: int, default: 0, reducer: replace}
    nodes:
      a: {uses: operation:a-op, state_writes: {x: "result.x"}}
      b: {uses: operation:b-op, when: "state.x == 1"}
    edges:
      - {from: START, to: a}
      - {from: START, to: b}
      - {from: a, to: END}
      - {from: b, to: END}
"""

ARTIFACT_GRAPH = """
schema_version: "2"
name: planner-artifact
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 8
    nodes:
      gen:
        uses: operation:gen-op
        outputs: [change:out/report.json]
      use: {uses: operation:use-op, when: "report.score >= 4"}
    edges:
      - {from: START, to: gen}
      - {from: gen, to: use}
      - {from: use, to: END}
"""

RETRY_GRAPH = """
schema_version: "2"
name: planner-retry
entrypoints:
  full: {graph: main}
policies:
  retry:
    transient: {max_attempts: 3, retry_on: [internal]}
graphs:
  main:
    max_supersteps: 8
    nodes:
      a: {uses: operation:a-op, retry: transient}
    edges:
      - {from: START, to: a}
      - {from: a, to: END}
"""

PRIORITY_GRAPH = """
schema_version: "2"
name: planner-priority
entrypoints:
  full: {graph: main}
policies:
  retry:
    transient: {max_attempts: 3, retry_on: [internal]}
graphs:
  main:
    max_supersteps: 8
    nodes:
      a: {uses: operation:a-op, retry: transient}
      review: {uses: operation:review-op}
    edges:
      - {from: START, to: a}
      - {from: START, to: review}
      - {from: a, to: END}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
"""


class _FakeArtifacts:
    """dict 支撑的 ArtifactReader：缺失路径抛 KeyError（按 MISSING fail-closed）。"""

    def __init__(self, payloads: dict[tuple[str, str], object] | None = None) -> None:
        self._payloads = payloads or {}
        self.calls: list[tuple[str, str]] = []

    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        self.calls.append((tree_id, logical_path))
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
    supersteps: int = 0,
    state_values: dict[str, object] | None = None,
    current_tree_id: str = "tree-0",
    latest_checkpoint_id: str | None = None,
    interrupts: dict[str, InterruptProjection] | None = None,
    terminal: str | None = None,
    terminal_reason: str | None = None,
    graph_digest: str | None = None,
) -> GraphProjection:
    return GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest=graph_digest if graph_digest is not None else compiled.digest,
        contract_digests={},
        params=params or {},
        root_tree_id="tree-0",
        current_tree_id=current_tree_id,
        latest_checkpoint_id=latest_checkpoint_id,
        supersteps=supersteps,
        state_values=state_values or {},
        tasks={task.task_id: task for task in tasks or []},
        interrupts=interrupts or {},
        terminal=terminal,  # type: ignore[arg-type]
        terminal_reason=terminal_reason,
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


# ---------------------------------------------------------------------------
# Step 1：激活与同 step 可见性


def test_initial_plan_returns_siblings_in_declaration_order(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    plan = _plan(compiled, _projection(compiled), tmp_path)

    assert [task.node_id for task in plan.tasks] == ["left", "right"]
    assert plan.terminal is None
    assert all(len(task.task_id) == 64 for task in plan.tasks)
    assert all(all(c in "0123456789abcdef" for c in task.task_id) for task in plan.tasks)

    replay = _plan(compiled, _projection(compiled), tmp_path)
    assert [task.task_id for task in replay.tasks] == [task.task_id for task in plan.tasks]
    assert [e.model_dump_json() for e in replay.strict_events] == [
        e.model_dump_json() for e in plan.strict_events
    ]

    activated = [e for e in plan.strict_events if isinstance(e, NodeActivatedEvent)]
    assert [e.node_id for e in activated] == ["left", "right"]
    planned = [e for e in plan.strict_events if isinstance(e, SuperstepPlannedEvent)]
    assert len(planned) == 1
    assert planned[0].task_ids == [task.task_id for task in plan.tasks]
    assert isinstance(plan.strict_events[-1], SuperstepPlannedEvent)


def test_single_success_unblocks_nothing(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    tasks = _initial_tasks(compiled, tmp_path)
    projection = _projection(
        compiled,
        tasks=[_task(tasks["left"], "succeeded"), _task(tasks["right"], "running")],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert plan.tasks == ()
    assert plan.strict_events == ()
    assert plan.terminal is None


def test_both_successes_make_join_ready(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    tasks = _initial_tasks(compiled, tmp_path)
    projection = _projection(
        compiled,
        tasks=[_task(tasks["left"], "succeeded"), _task(tasks["right"], "succeeded")],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["join"]
    assert plan.tasks[0].target == "builtin:join"
    activated = [e for e in plan.strict_events if isinstance(e, NodeActivatedEvent)]
    assert [e.node_id for e in activated] == ["join"]


@pytest.mark.parametrize("mode", ["a", "b"])
def test_mutually_exclusive_start_edges_activate_once(tmp_path: Path, mode: str) -> None:
    compiled = _compile(EXCLUSIVE_START)
    plan = _plan(compiled, _projection(compiled, params={"mode": mode}), tmp_path)
    assert [task.node_id for task in plan.tasks] == ["pick"]
    activated = [e for e in plan.strict_events if isinstance(e, NodeActivatedEvent)]
    assert [e.node_id for e in activated] == ["pick"]

    # pick 在飞行：重放 Plan 不产生第二次激活。
    running = _projection(
        compiled,
        params={"mode": mode},
        tasks=[_task(plan.tasks[0], "running")],
    )
    assert _plan(compiled, running, tmp_path).tasks == ()


def test_non_exclusive_convergence_rejected_not_duplicated(tmp_path: Path) -> None:
    compiled = _compile(NON_EXCLUSIVE)
    tasks = _initial_tasks(compiled, tmp_path)
    assert [t.node_id for t in tasks.values()] == ["left", "right"]

    # 两条非互斥路径在同一决策点同时选中普通 node：拒绝（fail closed），绝不重复激活。
    both = _projection(
        compiled,
        tasks=[_task(tasks["left"], "succeeded"), _task(tasks["right"], "succeeded")],
    )
    with pytest.raises(PlanError, match="builtin:join"):
        _plan(compiled, both, tmp_path)

    # 只有一条路径选中：OR 语义正常激活一次；其后另一条路径 settle 也不再激活。
    left_only = _projection(
        compiled,
        tasks=[_task(tasks["left"], "succeeded"), _task(tasks["right"], "running")],
    )
    plan = _plan(compiled, left_only, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["gather"]
    gather = plan.tasks[0]
    settled = _projection(
        compiled,
        tasks=[
            _task(tasks["left"], "succeeded"),
            _task(tasks["right"], "succeeded"),
            _task(gather, "running"),
        ],
    )
    assert _plan(compiled, settled, tmp_path).tasks == ()


# ---------------------------------------------------------------------------
# Step 3：冻结激活与 route


def test_skipped_node_does_not_traverse_outgoing_edges(tmp_path: Path) -> None:
    text = """
schema_version: "2"
name: planner-skip-chain
params:
  flag: {type: bool, default: false}
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 4
    nodes:
      b: {uses: operation:b-op, when: "params.flag == true"}
      c: {uses: operation:c-op}
    edges:
      - {from: START, to: b}
      - {from: b, to: c}
      - {from: c, to: END}
"""
    compiled = _compile(text)
    plan = _plan(compiled, _projection(compiled, params={"flag": False}), tmp_path)
    assert plan.tasks == ()
    skipped = [e for e in plan.strict_events if isinstance(e, NodeSkippedEvent)]
    assert [e.node_id for e in skipped] == ["b", "c"]
    assert skipped[0].expression == "params.flag == true"


def test_route_selects_case_default_and_fail_closed(tmp_path: Path) -> None:
    compiled = _compile(ROUTE_GRAPH)
    review = _initial_tasks(compiled, tmp_path)["review"]

    needs_fix = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "needs_fix"})],
    )
    plan = _plan(compiled, needs_fix, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["fix"]

    passed = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "pass"})],
    )
    end_plan = _plan(compiled, passed, tmp_path)
    assert end_plan.tasks == ()
    assert end_plan.terminal == "end"

    unmatched = _projection(
        compiled,
        tasks=[_task(review, "succeeded", gate_report={"verdict": "reject"})],
    )
    stop_plan = _plan(compiled, unmatched, tmp_path)
    assert stop_plan.terminal == "stop"
    assert stop_plan.tasks == ()

    # select MISSING（gate_report 为空）且无 default：fail closed 到 STOP。
    no_default = _compile(ROUTE_NO_DEFAULT)
    review_nd = _initial_tasks(no_default, tmp_path)["review"]
    missing = _projection(no_default, tasks=[_task(review_nd, "succeeded")])
    closed = _plan(no_default, missing, tmp_path)
    assert closed.terminal == "stop"
    assert closed.tasks == ()


def test_route_missing_selection_uses_explicit_default(tmp_path: Path) -> None:
    text = """
schema_version: "2"
name: planner-route-default
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 4
    nodes:
      review: {uses: operation:review-op}
      fix: {uses: operation:fix-op}
    edges:
      - {from: START, to: review}
      - {from: fix, to: END}
    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
        default: fix
"""
    compiled = _compile(text)
    review = _initial_tasks(compiled, tmp_path)["review"]
    projection = _projection(compiled, tasks=[_task(review, "succeeded")])
    plan = _plan(compiled, projection, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["fix"]


def test_sibling_pending_state_is_invisible_until_commit(tmp_path: Path) -> None:
    compiled = _compile(STATE_GRAPH)
    tasks = _initial_tasks(compiled, tmp_path)

    # a 已成功并携带 pending state_updates，但 superstep 尚未 commit：
    # b 的 when 只能读 projection.state_values（仍是默认 0）。
    pending = _projection(
        compiled,
        tasks=[_task(tasks["a"], "succeeded", state_updates={"x": 1})],
    )
    plan = _plan(compiled, pending, tmp_path)
    assert all(task.node_id != "b" for task in plan.tasks)
    skipped = [e for e in plan.strict_events if isinstance(e, NodeSkippedEvent)]
    assert [e.node_id for e in skipped] == ["b"]

    # commit 之后 state.x == 1 对下一次 Plan 可见。
    committed = _projection(
        compiled,
        state_values={"x": 1},
        tasks=[_task(tasks["a"], "succeeded", state_updates={"x": 1})],
    )
    assert [task.node_id for task in _plan(compiled, committed, tmp_path).tasks] == ["b"]


def test_artifact_scope_reads_only_current_tree_and_freezes_hashes(tmp_path: Path) -> None:
    compiled = _compile(ARTIFACT_GRAPH)
    gen = _initial_tasks(compiled, tmp_path)["gen"]
    artifacts = _FakeArtifacts({("tree-1", "change:out/report.json"): {"score": 5}})
    projection = _projection(
        compiled,
        current_tree_id="tree-1",
        tasks=[_task(gen, "succeeded")],
    )
    plan = _plan(compiled, projection, tmp_path, artifacts)
    assert [task.node_id for task in plan.tasks] == ["use"]
    assert ("tree-1", "change:out/report.json") in artifacts.calls
    activated = [e for e in plan.strict_events if isinstance(e, NodeActivatedEvent)]
    expected_sha = hashlib.sha256(b'{"score":5}').hexdigest()
    assert activated[0].source_reads_sha256 == {"change:out/report.json": expected_sha}

    # 低分 skip；artifact 缺失按 MISSING fail closed，同样 skip。
    low = _FakeArtifacts({("tree-1", "change:out/report.json"): {"score": 1}})
    low_plan = _plan(compiled, projection, tmp_path, low)
    assert all(task.node_id != "use" for task in low_plan.tasks)
    missing_plan = _plan(compiled, projection, tmp_path, _FakeArtifacts())
    assert all(task.node_id != "use" for task in missing_plan.tasks)


# ---------------------------------------------------------------------------
# Step 4：join 模式


def test_join_all_waits_for_succeeded_or_skipped_sources(tmp_path: Path) -> None:
    compiled = _compile(JOIN_GRAPH.replace("__MODE__", "all"))
    params: dict[str, object] = {"go": True, "flag": False}
    initial = _plan(compiled, _projection(compiled, params=params), tmp_path)
    assert [task.node_id for task in initial.tasks] == ["a"]
    skipped = [e for e in initial.strict_events if isinstance(e, NodeSkippedEvent)]
    assert [e.node_id for e in skipped] == ["b"]

    a_done = _projection(compiled, params=params, tasks=[_task(initial.tasks[0], "succeeded")])
    plan = _plan(compiled, a_done, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["j"]


def test_join_all_active_ignores_skipped_sources(tmp_path: Path) -> None:
    compiled = _compile(JOIN_GRAPH.replace("__MODE__", "all_active"))
    params: dict[str, object] = {"go": True, "flag": False}
    initial = _plan(compiled, _projection(compiled, params=params), tmp_path)
    a_done = _projection(compiled, params=params, tasks=[_task(initial.tasks[0], "succeeded")])
    plan = _plan(compiled, a_done, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["j"]


def test_join_all_active_empty_is_error_not_implicit_pass(tmp_path: Path) -> None:
    compiled = _compile(JOIN_GRAPH.replace("__MODE__", "all_active"))
    params: dict[str, object] = {"go": False, "flag": False}
    with pytest.raises(PlanError, match="all_active"):
        _plan(compiled, _projection(compiled, params=params), tmp_path)


def test_join_any_ready_after_first_success(tmp_path: Path) -> None:
    compiled = _compile(JOIN_GRAPH.replace("__MODE__", "any"))
    params: dict[str, object] = {"go": True, "flag": True}
    initial = _plan(compiled, _projection(compiled, params=params), tmp_path)
    by_node = {task.node_id: task for task in initial.tasks}
    projection = _projection(
        compiled,
        params=params,
        tasks=[_task(by_node["a"], "succeeded"), _task(by_node["b"], "running")],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.node_id for task in plan.tasks] == ["j"]


def test_join_any_all_sources_skipped_skips_join(tmp_path: Path) -> None:
    compiled = _compile(JOIN_GRAPH.replace("__MODE__", "any"))
    params: dict[str, object] = {"go": False, "flag": False}
    plan = _plan(compiled, _projection(compiled, params=params), tmp_path)
    assert plan.tasks == ()
    skipped = [e for e in plan.strict_events if isinstance(e, NodeSkippedEvent)]
    assert [e.node_id for e in skipped] == ["a", "b", "j"]


# ---------------------------------------------------------------------------
# Step 6：max-superstep 与终局优先级


def test_max_supersteps_returns_fail_before_planning(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    projection = _projection(compiled, supersteps=8)
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == "fail"
    assert plan.tasks == ()
    assert plan.reason is not None and "max_supersteps" in plan.reason


@pytest.mark.parametrize(
    ("projected", "expected"),
    [("completed", "end"), ("stopped", "stop"), ("failed", "fail")],
)
def test_already_terminal_projection_is_replayed(tmp_path: Path, projected: str, expected: str) -> None:
    compiled = _compile(DIAMOND)
    projection = _projection(compiled, terminal=projected, terminal_reason="done")
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == expected
    assert plan.reason == "done"
    assert plan.tasks == ()


def test_failed_retryable_task_is_replanned_with_same_task_id(tmp_path: Path) -> None:
    compiled = _compile(RETRY_GRAPH)
    original = _initial_tasks(compiled, tmp_path)["a"]
    projection = _projection(
        compiled,
        tasks=[_task(original, "failed", error_kind="internal", attempts_used=1)],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.task_id for task in plan.tasks] == [original.task_id]
    assert plan.terminal is None
    planned = [e for e in plan.strict_events if isinstance(e, SuperstepPlannedEvent)]
    assert planned[0].task_ids == [original.task_id]


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        ("failed", {"error_kind": "internal", "attempts_used": 3}),
        ("failed", {"error_kind": "auth", "attempts_used": 1}),
        ("abandoned", {"attempts_used": 3}),
    ],
)
def test_non_retryable_or_exhausted_failure_is_terminal_fail(
    tmp_path: Path, status: str, overrides: dict[str, object]
) -> None:
    compiled = _compile(RETRY_GRAPH)
    original = _initial_tasks(compiled, tmp_path)["a"]
    projection = _projection(compiled, tasks=[_task(original, status, **overrides)])
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == "fail"
    assert plan.tasks == ()


def test_abandoned_with_remaining_budget_is_replanned(tmp_path: Path) -> None:
    compiled = _compile(RETRY_GRAPH)
    original = _initial_tasks(compiled, tmp_path)["a"]
    projection = _projection(
        compiled,
        tasks=[_task(original, "abandoned", attempts_used=1)],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert [task.task_id for task in plan.tasks] == [original.task_id]
    assert plan.terminal is None


def test_stop_outranks_retry_pending(tmp_path: Path) -> None:
    compiled = _compile(PRIORITY_GRAPH)
    initial = _plan(compiled, _projection(compiled), tmp_path)
    by_node = {task.node_id: task for task in initial.tasks}
    projection = _projection(
        compiled,
        tasks=[
            _task(by_node["a"], "failed", error_kind="internal", attempts_used=1),
            _task(by_node["review"], "succeeded", gate_report={"verdict": "reject"}),
        ],
    )
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == "stop"
    assert plan.tasks == ()


def test_pending_interrupt_outranks_ready_tasks(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    interrupt = InterruptProjection(
        interrupt_id="i-1",
        checkpoint_ns="inv-1",
        node_id="left",
        checkpoint="main.left",
        actions=("stop",),
        audited_reads_sha256={},
    )
    projection = _projection(compiled, interrupts={"i-1": interrupt})
    plan = _plan(compiled, projection, tmp_path)
    assert plan.terminal == "interrupt"
    assert plan.tasks == ()

    resolved = interrupt.model_copy(update={"resolved_action": "stop"})
    resumed = _projection(compiled, interrupts={"i-1": resolved})
    assert [task.node_id for task in _plan(compiled, resumed, tmp_path).tasks] == ["left", "right"]


def test_graph_digest_drift_fails_closed(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    projection = _projection(compiled, graph_digest="0" * 64)
    with pytest.raises(PlanError, match="graph_definition_changed"):
        _plan(compiled, projection, tmp_path)


def test_checkpoint_id_tracks_latest_or_bootstrap(tmp_path: Path) -> None:
    compiled = _compile(DIAMOND)
    bootstrap = _plan(compiled, _projection(compiled), tmp_path)
    assert bootstrap.checkpoint_id == "bootstrap-inv-1"
    with_cp = _plan(compiled, _projection(compiled, latest_checkpoint_id="cp-9"), tmp_path)
    assert with_cp.checkpoint_id == "cp-9"
    assert with_cp.superstep_id == bootstrap.superstep_id


# ---------------------------------------------------------------------------
# Step 5：typed reducer


def _defs(**entries: tuple[str, object, str]) -> dict[str, StateDef]:
    return {
        key: StateDef(type=type_, default=default, reducer=reducer)  # type: ignore[arg-type]
        for key, (type_, default, reducer) in entries.items()
    }


def test_replace_allows_single_writer_per_superstep() -> None:
    defs = _defs(k=("int", 0, "replace"))
    assert apply_state_updates(defs, {"k": 0}, [("t-1", {"k": 41})]) == {"k": 41}
    with pytest.raises(PlanError, match="multiple writers"):
        apply_state_updates(defs, {"k": 0}, [("t-1", {"k": 1}), ("t-2", {"k": 2})])


def test_append_requires_lists_and_orders_by_task_id() -> None:
    defs = _defs(items=("list", [], "append"))
    merged = apply_state_updates(defs, {"items": [1]}, [("t-2", {"items": [3]}), ("t-1", {"items": [2]})])
    assert merged == {"items": [1, 2, 3]}
    with pytest.raises(PlanError, match="append"):
        apply_state_updates(defs, {"items": []}, [("t-1", {"items": "not-a-list"})])


def test_merge_disjoint_rejects_duplicate_keys() -> None:
    defs = _defs(summary=("object", {}, "merge_disjoint"))
    merged = apply_state_updates(defs, {"summary": {"a": 1}}, [("t-1", {"summary": {"b": 2}})])
    assert merged == {"summary": {"a": 1, "b": 2}}
    with pytest.raises(PlanError, match="duplicate object keys"):
        apply_state_updates(defs, {"summary": {"a": 1}}, [("t-1", {"summary": {"a": 9}})])


def test_set_union_dedupes_and_sorts_by_canonical_json() -> None:
    defs = _defs(tags=("list", [], "set_union"))
    merged = apply_state_updates(
        defs,
        {"tags": ["beta"]},
        [("t-1", {"tags": ["alpha", "beta"]}), ("t-2", {"tags": ["gamma"]})],
    )
    assert merged == {"tags": ["alpha", "beta", "gamma"]}
    with pytest.raises(PlanError, match="set_union"):
        apply_state_updates(defs, {"tags": []}, [("t-1", {"tags": "nope"})])


def test_state_updates_reject_undeclared_keys() -> None:
    defs = _defs(k=("int", 0, "replace"))
    with pytest.raises(PlanError, match="undeclared state key"):
        apply_state_updates(defs, {}, [("t-1", {"unknown": 1})])
