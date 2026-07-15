"""DAG 状态引擎（忠实转录 engine.ts）：produces-存在性 + gate 裁决 驱动进度；纯函数。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied, parse_expression
from assurance_agent.workflow.orchestration.gates import (
    build_evidence_scope,
    resolve_change_path,
    resolve_gate_verdict,
)
from assurance_agent.workflow.orchestration.healing_episode import (
    HealingEpisodeSnapshot,
    project_healing_episode,
)
from assurance_agent.workflow.orchestration.healing_state import (
    HealingStateProvider,
    HealingStateSnapshot,
    derive_healing_state,
)
from assurance_agent.workflow.orchestration.schema import PhaseDef, ReadEntry, Verdict, WorkflowSchema

# 调度时视为「编排器内置」的 phase（对齐源版 resolveNextDispatch 的 internal 分支）。
# 注意：这与 schema.py 的 ORCHESTRATOR_INTERNAL（agent 豁免校验，仅 skill-registry-check）是
# 两个不同用途的集合，故此处独立命名、不复用 schema 的常量。
_INTERNAL_PHASES = {"skill-registry-check", "test-infra-bootstrap"}

_TERMINAL_VERDICTS = {Verdict.STOP, Verdict.REJECT}


class DispatchEntry(BaseModel):
    phase_id: str
    skill: str | None = None
    agent: str | None = None
    kind: Literal["skill", "cli", "orchestrator"]


class PhaseView(BaseModel):
    id: str
    # pruned | out_of_scope | blocked | ready | awaiting_gate | done | stopped（对齐源版词汇）
    status: str
    gate: str | None = None
    gate_verdict: str | None = None
    produces_present: bool = False


class Terminal(BaseModel):
    kind: Literal["completed", "stopped", "needs_human_review"]
    reason: str | None = None
    phase: str | None = None


class WorkflowStatus(BaseModel):
    phases: list[PhaseView]
    next_dispatch: list[DispatchEntry]
    terminal: Terminal | None = None
    healing_episode: HealingEpisodeSnapshot = Field(
        default_factory=lambda: HealingEpisodeSnapshot(state="inactive", stage=None)
    )


def _event_seq(event: dict[str, object]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


def _dispatch_kind(phase: PhaseDef) -> Literal["skill", "cli", "orchestrator"]:
    if phase.id in _INTERNAL_PHASES:
        return "orchestrator"
    return "cli" if phase.skill is None else "skill"


def _in_active_scope(phase: PhaseDef, active_scope: str | None) -> bool:
    # active_scope=None（full）→ 全部在内；否则按 owned_by 归属。
    if active_scope is None or phase.owned_by is None:
        return True
    return active_scope in phase.owned_by


def _file_exists(change_dir: Path, rel: str) -> bool:
    p = resolve_change_path(change_dir, rel)
    if rel.endswith("/"):  # 目录 produces：需存在且非空（对齐源版 fileExists 目录分支）
        return p.is_dir() and any(p.iterdir())
    return p.exists()


def _produces_present(change_dir: Path, phase: PhaseDef) -> bool:
    return bool(phase.produces) and all(_file_exists(change_dir, r) for r in phase.produces)


def _topo_order(schema: WorkflowSchema) -> list[str]:
    """Kahn 拓扑排序；同层按声明顺序稳定输出。schema 加载期已校验无环。"""
    ids = [p.id for p in schema.phases]
    indeg = {i: 0 for i in ids}
    deps: dict[str, list[str]] = {p.id: [d for d in p.requires if d in indeg] for p in schema.phases}
    for i in ids:
        indeg[i] = len(deps[i])
    order: list[str] = []
    ready = [i for i in ids if indeg[i] == 0]  # 保持声明顺序
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for p in schema.phases:  # 声明顺序扫描，稳定
            if cur in deps[p.id]:
                indeg[p.id] -= 1
                if indeg[p.id] == 0:
                    ready.append(p.id)
    if len(order) != len(ids):
        raise AssertionError("phase DAG cycle escaped schema validation")
    return order


def _overlay_healing(
    state: WorkflowState,
    change_dir: Path,
    provider: HealingStateProvider | None,
) -> tuple[WorkflowState, HealingStateSnapshot]:
    """Overlay event-derived state on a copy; persisted attempts never win."""
    derived = (provider or derive_healing_state)(change_dir)
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases["healing"] = {
        **dict(phases.get("healing") or {}),
        **derived.model_dump(mode="python", exclude_none=True),
    }
    data["phases"] = phases
    return WorkflowState.model_validate(data), derived


def compute_status(
    schema: WorkflowSchema,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    *,
    scope: str = "full",
    healing_provider: HealingStateProvider | None = None,
) -> WorkflowStatus:
    merged_params = {**schema.default_param_values(), **params}
    state, derived_healing = _overlay_healing(state, change_dir, healing_provider)
    active_scope = None if scope == "full" else scope
    memo: dict[str, Verdict] = {}  # 单次 compute_status 内共享 gate 裁决 memo

    # when/ready_when 的 predicate 作用域：全局反向别名（over 所有 produces），不 hoist primary。
    alias_reads = [ReadEntry(path=path, alias=alias) for alias, path in schema.produces_alias_map().items()]

    def predicate_scope():
        return build_evidence_scope(
            schema,
            change_dir,
            state,
            merged_params,
            alias_reads,
            hoist_primary=False,
            memo=memo,
        )

    def gate_verdict(gate_id: str) -> Verdict:
        return resolve_gate_verdict(schema, gate_id, change_dir, state, merged_params, memo, ())

    by_id = {p.id: p for p in schema.phases}

    # 1) 剪枝：when 非 True（含传递剪枝）
    pruned: dict[str, bool] = {}
    for phase in schema.phases:
        pruned[phase.id] = bool(phase.when) and not is_satisfied(
            parse_expression(phase.when), predicate_scope()
        )

    views: dict[str, PhaseView] = {}
    for pid in _topo_order(schema):
        phase = by_id[pid]
        views[pid] = _phase_view(
            by_id,
            change_dir,
            phase,
            pruned,
            views,
            active_scope,
            predicate_scope,
            gate_verdict,
        )

    phases = [views[p.id] for p in schema.phases]  # 声明顺序输出
    ready = [
        DispatchEntry(
            phase_id=p.id, skill=by_id[p.id].skill, agent=by_id[p.id].agent, kind=_dispatch_kind(by_id[p.id])
        )
        for p in phases
        if p.status == "ready"
    ]
    terminal = _terminal(phases, ready)

    healing_loop = schema.loops.get("healing")
    loop_members = set(healing_loop.members if healing_loop else [])
    episode = project_healing_episode(schema, change_dir, state, merged_params, derived_healing)
    ready = [d for d in ready if d.phase_id not in loop_members]
    for action in episode.next_actions:
        if action.kind == "dispatch_phase" and action.phase:
            phase = by_id[action.phase]
            views[action.phase] = views[action.phase].model_copy(update={"status": "ready"})
            ready.append(
                DispatchEntry(
                    phase_id=phase.id,
                    skill=phase.skill,
                    agent=phase.agent,
                    kind=_dispatch_kind(phase),
                )
            )
    latest_decision = max(
        (e for e in read_events(change_dir) if e.get("type") == "human_decision"),
        key=_event_seq,
        default=None,
    )
    if latest_decision is not None and latest_decision.get("action") == "stop":
        terminal = Terminal(
            kind="stopped",
            reason=str(latest_decision.get("reason") or "stopped by human decision"),
            phase=str(latest_decision.get("checkpoint") or "workflow"),
        )
    elif episode.terminal_kind == "stopped":
        terminal = Terminal(kind="stopped", reason=episode.reason, phase="healing")
    elif episode.state in {"active", "awaiting_human"}:
        terminal = None
    next_dispatch = [] if terminal else ready
    return WorkflowStatus(
        phases=[views[p.id] for p in schema.phases],
        next_dispatch=next_dispatch,
        terminal=terminal,
        healing_episode=episode,
    )


def _phase_view(
    by_id: dict[str, PhaseDef],
    change_dir: Path,
    phase: PhaseDef,
    pruned: dict[str, bool],
    views: dict[str, PhaseView],
    active_scope: str | None,
    predicate_scope: Callable[[], Scope],
    gate_verdict: Callable[[str], Verdict],
) -> PhaseView:
    gate = phase.gate

    if pruned[phase.id]:
        return PhaseView(id=phase.id, status="pruned", gate=gate)

    active_deps = [d for d in phase.requires if views[d].status not in ("pruned", "out_of_scope")]
    if phase.requires and all(views[d].status == "pruned" for d in phase.requires):
        return PhaseView(id=phase.id, status="pruned", gate=gate)

    produces_present = _produces_present(change_dir, phase)

    if not produces_present and not _in_active_scope(phase, active_scope):
        return PhaseView(id=phase.id, status="out_of_scope", gate=gate)

    def is_done(dep_id: str) -> bool:
        if views[dep_id].status == "done":
            return True
        # repair 特例：本 phase 修复 dep，且 dep 的 exit gate 当前判 needs_fix → 视 dep「已就绪待修」
        if phase.repair_of != dep_id:
            return False
        dep = by_id.get(dep_id)
        if dep and dep.gate:
            return gate_verdict(dep.gate) == "needs_fix"
        return False

    done_deps = [d for d in active_deps if is_done(d)]
    if phase.requires_mode == "any_active":
        blocked = bool(active_deps) and not done_deps
    else:
        blocked = any(not is_done(d) for d in active_deps)
    if blocked:
        return PhaseView(id=phase.id, status="blocked", gate=gate, produces_present=produces_present)

    # repair 路由：目标 phase 判 needs_fix → 拉起本 repair phase
    if phase.repair_of:
        target = by_id.get(phase.repair_of)
        if target and target.gate and gate_verdict(target.gate) == "needs_fix":
            return PhaseView(id=phase.id, status="ready", gate=gate, produces_present=produces_present)

    if not produces_present:
        if phase.ready_when and not is_satisfied(parse_expression(phase.ready_when), predicate_scope()):
            return PhaseView(id=phase.id, status="blocked", gate=gate)
        return PhaseView(id=phase.id, status="ready", gate=gate, produces_present=False)

    # produces 已生成 → 已运行：无 gate 即 done；有 gate 按裁决路由（gate 只在此处裁决）
    if not gate:
        return PhaseView(id=phase.id, status="done", produces_present=True)
    verdict = gate_verdict(gate)
    if verdict in _TERMINAL_VERDICTS:
        status = "stopped"
    elif verdict == "pass":
        status = "done"
    else:  # needs_fix / needs_human_review / 其它非终局 → 挂起
        status = "awaiting_gate"
    return PhaseView(id=phase.id, status=status, gate=gate, gate_verdict=verdict, produces_present=True)


def _terminal(phases: list[PhaseView], ready: list[DispatchEntry]) -> Terminal | None:
    stopped = next((p for p in phases if p.status == "stopped"), None)
    if stopped is not None:
        return Terminal(
            kind="stopped",
            phase=stopped.id,
            reason=f"gate '{stopped.gate}' verdict '{stopped.gate_verdict}'",
        )
    if ready:
        return None  # 还有可推进项，优先推进（needs_human_review 分支不阻断并行 ready）
    pending = next(
        (p for p in phases if p.status == "awaiting_gate" and p.gate_verdict == "needs_human_review"),
        None,
    )
    if pending is not None:
        return Terminal(
            kind="needs_human_review",
            phase=pending.id,
            reason=f"gate '{pending.gate}' needs human review",
        )
    active = [p for p in phases if p.status not in ("pruned", "out_of_scope")]
    if active and all(p.status == "done" for p in active):
        return Terminal(kind="completed")
    return None  # 普通 DAG 尚无结论；Task 9B 的 episode projection 再做 loop terminal 汇聚
