"""Gate 四态裁决：读取 evidence，按规则顺序求值，输出单一 verdict。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    Scope,
    evaluate,
    parse_expression,
)
from assurance_agent.workflow.orchestration.schema import GateDef, ReadEntry, Verdict, WorkflowSchema


class GateVerdict(BaseModel):
    gate: str
    verdict: Verdict
    matched_rule: str | None = None
    reason: str | None = None


class GateCycleError(AaError):
    """gate() 递归裁决出现环。"""


def _project_root(change_dir: Path) -> Path:
    # change_dir = <root>/qa/changes/<id>
    return change_dir.parents[2] if len(change_dir.parents) >= 3 else change_dir


def resolve_change_path(change_dir: Path, rel: str) -> Path:
    """schema 路径 → 绝对路径：`repo:` / `qa/` 前缀相对项目根，否则相对 change 目录。

    对齐源版 `resolvePath`。供 gate reads 与引擎 produces-existence 共用。
    """
    root = _project_root(change_dir)
    change_id = change_dir.name
    normalized = rel.replace("<change-id>", change_id)
    if normalized.startswith("repo:"):
        return root / normalized[len("repo:"):]
    if normalized.startswith("qa/"):
        return root / normalized
    return change_dir / normalized


# 向后兼容别名（内部沿用）。
_resolve_path = resolve_change_path


def _load_doc(change_dir: Path, rel: str) -> tuple[bool, bool, object]:
    """returns (present, parse_error, value)."""
    path = _resolve_path(change_dir, rel)
    if not path.exists():
        return False, False, None
    try:
        text = path.read_text(encoding="utf-8")
        if rel.endswith((".yaml", ".yml")):
            return True, False, yaml.safe_load(text)
        return True, False, json.loads(text)
    except (json.JSONDecodeError, yaml.YAMLError, ValueError):
        return True, True, None


def _scope_state(state: WorkflowState) -> dict:
    # exclude_none 保留 DSL 的“未出现=MISSING”，同时保留 extra='allow' 扩展键。
    return state.model_dump(mode="json", exclude_none=True)


def build_evidence_scope(
    schema: WorkflowSchema,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    reads: list[ReadEntry] | None,
    *,
    hoist_primary: bool = True,
    memo: dict[str, Verdict] | None = None,
    stack: tuple[str, ...] = (),
) -> Scope:
    """构造 gate 规则与 phase when/ready_when **共用**的求值作用域（对齐源版 makeScope）。

    - **gate**（`hoist_primary=True`）：`reads[0]` primary 文档 hoist 顶层字段 + 各 read alias
      文档 + `params` + `state` + 内建 `file_exists()`/`gate()`。
    - **phase when/ready_when / loop allocate_on**（`hoist_primary=False`）：引擎传入
      `schema.produces_alias_map()` 派生的**全局反向别名** ReadEntry 列表（对齐源版
      `reverseAlias` / `predicateScope`，**不 hoist**），因此 `any(fix_proposal.proposals, ...)`、
      `gate('healing-entry-gate').verdict == 'enter'` 等打包 schema 条件在编排期即可求值——
      **无需任何 per-phase `reads` 字段**。
    """
    reads = reads or []
    alias_docs: dict[str, object] = {}
    for r in reads:
        _, _, val = _load_doc(change_dir, r.path)
        alias_docs[r.alias] = val
    primary_val = alias_docs.get(reads[0].alias) if (reads and hoist_primary) else None
    hoisted = primary_val if isinstance(primary_val, dict) else {}
    scope_vars: dict[str, object] = {
        **hoisted,
        **alias_docs,
        "params": params,
        "state": _scope_state(state),
    }
    _memo = memo if memo is not None else {}

    def file_exists(rel: str) -> bool:
        return _resolve_path(change_dir, rel).exists()

    def gate_verdict(gid: str) -> str:
        try:
            return resolve_gate_verdict(schema, gid, change_dir, state, params, _memo, stack).value
        except GateCycleError:
            return Verdict.STOP.value

    return Scope(scope_vars, file_exists=file_exists, gate_verdict=gate_verdict)


def resolve_gate_verdict(
    schema: WorkflowSchema,
    gate_name: str,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    _memo: dict[str, Verdict] | None = None,
    _stack: tuple[str, ...] = (),
) -> Verdict:
    memo = _memo if _memo is not None else {}
    if gate_name in memo:
        return memo[gate_name]
    if gate_name in _stack:
        raise GateCycleError("gate reference cycle: " + " -> ".join([*_stack, gate_name]))
    gate = schema.gates.get(gate_name)
    if gate is None:
        return Verdict.STOP
    verdict = _adjudicate(schema, gate, change_dir, state, params, memo, (*_stack, gate_name))[0]
    memo[gate_name] = verdict
    return verdict


def _adjudicate(
    schema: WorkflowSchema,
    gate: GateDef,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    memo: dict[str, Verdict],
    stack: tuple[str, ...],
) -> tuple[Verdict, str | None]:
    # Step 1 — invalid_json: stop
    if gate.invalid_json == Verdict.STOP:
        for r in gate.reads:
            present, parse_error, _ = _load_doc(change_dir, r.path)
            if present and parse_error:
                return Verdict.STOP, "invalid_json"

    # Step 2 — build scope (gate: primary hoist + aliases; shared helper with engine)
    scope = build_evidence_scope(schema, change_dir, state, params, gate.reads, memo=memo, stack=stack)

    # Step 3 — rules in DECLARATION order, first-true-wins (对齐源版 adjudicate)。
    # 安全语义（needs_fix→needs_human_review→reject→pass）由 schema 加载期强制的
    # canonical 声明顺序保证（Task 4 `_SAFETY_ORDER` 校验），因此声明顺序 == 安全优先级，
    # 无需运行期再排序（避免与源版行为分叉）。
    saw_missing = False
    for rule in gate.rules:
        result = evaluate(parse_expression(rule.expr), scope)
        if result is True:
            return rule.verdict, f"{rule.field}: {rule.expr}"
        if result is MISSING:
            saw_missing = True

    # Step 4 — missing_field_is
    if saw_missing and gate.missing_field_is:
        return gate.missing_field_is, "missing_field"

    # Step 5 — missing_file_is
    any_missing = any(not _resolve_path(change_dir, r.path).exists() for r in gate.reads)
    if any_missing and gate.missing_file_is:
        return gate.missing_file_is, "missing_file"

    # Step 6 — fail-closed default
    return gate.default, None


def check_gate(
    schema: WorkflowSchema,
    gate_name: str,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
) -> GateVerdict:
    gate = schema.gates.get(gate_name)
    if gate is None:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason="unknown gate")
    memo: dict[str, Verdict] = {}
    try:
        verdict, matched = _adjudicate(schema, gate, change_dir, state, params, memo, (gate_name,))
    except GateCycleError as exc:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason=str(exc))
    return GateVerdict(gate=gate_name, verdict=verdict, matched_rule=matched)
