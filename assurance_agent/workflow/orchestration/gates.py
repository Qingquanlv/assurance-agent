"""Gate 四态裁决：读取 evidence，按规则顺序求值，输出单一 verdict。"""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.change_location import ChangeLocation
from assurance_agent.exceptions import AaError
from assurance_agent.knowledge.capabilities import capabilities_present as check_capabilities_present
from assurance_agent.knowledge.capabilities import compute_missing_capabilities
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    Scope,
    evaluate,
    parse_expression,
)
from typing import Any, Protocol

from assurance_agent.workflow.orchestration.schema import GateDef, ReadEntry, Verdict


class _SchemaWithGates(Protocol):
    @property
    def gates(self) -> Mapping[str, GateDef]: ...


# Human decisions that can upgrade a `needs_human_review` gate verdict. Mirror of
# TS ``applyGateDecision`` (engine.ts): ``accept_risk``→pass, ``fix_and_proceed``
# →needs_fix. Both require the decision to carry ``review_file``/``review_sha256``
# anchoring an audited gate read whose current hash still matches.
_GATE_DECISION_ACTIONS = frozenset({"accept_risk", "fix_and_proceed"})


# Shared checkpoint→gate alias table (also consumed by InterruptHandler).
# Do not create a second alias table elsewhere.
CHECKPOINT_GATE_ALIASES: Mapping[str, str] = MappingProxyType({"healing.safety": "fixer-safety-gate"})


def resolve_checkpoint_gate_id(checkpoint: str, gate_ids: frozenset[str]) -> str | None:
    """Resolve a checkpoint name to a compiled gate id via direct match or alias."""
    if checkpoint in gate_ids:
        return checkpoint
    aliased = CHECKPOINT_GATE_ALIASES.get(checkpoint)
    if aliased is not None and aliased in gate_ids:
        return aliased
    return None


def _checkpoint_matches_gate(checkpoint: object, gate_id: str) -> bool:
    if not isinstance(checkpoint, str):
        return False
    if checkpoint == gate_id:
        return True
    return CHECKPOINT_GATE_ALIASES.get(checkpoint) == gate_id


def _decision_matches_source_epoch(
    decision: Mapping[str, object],
    *,
    source_gate_attempt_id: str | None,
    source_gate_tree_id: str | None,
    require_source_epoch: bool,
) -> bool:
    """v5 gate overrides must bind the source gate attempt and committed tree."""
    if not require_source_epoch:
        return True
    attempt = decision.get("source_gate_attempt_id")
    tree = decision.get("source_gate_tree_id")
    if attempt is None or tree is None:
        # Pairless resumes are normal actions but never gate overrides.
        return False
    if source_gate_attempt_id is None or source_gate_tree_id is None:
        return False
    return attempt == source_gate_attempt_id and tree == source_gate_tree_id


class GateVerdict(BaseModel):
    gate: str
    verdict: Verdict
    matched_rule: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AcceptedRiskDecision:
    """A graph-resume decision that is valid in one descendant checkpoint view."""

    gate_id: str
    interrupt_id: str
    checkpoint_ns: str
    reason: str
    who: str


class GateCycleError(AaError):
    """gate() 递归裁决出现环。"""


def resolve_change_path(loc: ChangeLocation, rel: str) -> Path:
    """schema 路径 → 绝对路径：`repo:` / `qa/` 前缀相对项目根，否则相对 change 目录。

    对齐源版 `resolvePath`。供 gate reads 与引擎 produces-existence 共用。项目根来自
    ``loc.project_root``（禁止 ``parents[2]`` 深度反推，见 ADR-0002）。
    """
    normalized = rel.replace("<change-id>", loc.change_id)
    if normalized.startswith("repo:"):
        return loc.project_root / normalized[len("repo:") :]
    if normalized.startswith("qa/"):
        return loc.project_root / normalized
    return loc.path / normalized


# 向后兼容别名（内部沿用）。
_resolve_path = resolve_change_path


def _load_doc(loc: ChangeLocation, rel: str) -> tuple[bool, bool, object]:
    """returns (present, parse_error, value)."""
    path = _resolve_path(loc, rel)
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
    schema: _SchemaWithGates,
    loc: ChangeLocation,
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
        _, _, val = _load_doc(loc, r.path)
        alias_docs[r.alias] = val
    primary_val = alias_docs.get(reads[0].alias) if (reads and hoist_primary) else None
    hoisted = primary_val if isinstance(primary_val, dict) else {}
    scope_vars: dict[str, object] = {
        **hoisted,
        **alias_docs,
        "params": params,
        "state": _scope_state(state),
        "policy": load_policy(loc.project_root).model_dump(mode="json"),
    }
    _memo = memo if memo is not None else {}

    def file_exists(rel: str) -> bool:
        return _resolve_path(loc, rel).exists()

    def gate_verdict(gid: str) -> str:
        try:
            return resolve_gate_verdict(schema, gid, loc, state, params, _memo, stack).value
        except GateCycleError:
            return Verdict.STOP.value

    def resolve_plan_assurance_state(
        checks: object, review: object, data_knowledge: object, layer: object
    ) -> str:
        return plan_assurance_state(
            checks,
            review,
            data_knowledge,
            layer,
            change_id=loc.change_id,
        )

    return Scope(
        scope_vars,
        file_exists=file_exists,
        gate_verdict=gate_verdict,
        capabilities_present=check_capabilities_present,
        plan_assurance_state=resolve_plan_assurance_state,
    )


def resolve_gate_verdict(
    schema: _SchemaWithGates,
    gate_name: str,
    loc: ChangeLocation,
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
    verdict = _adjudicate(schema, gate, loc, state, params, memo, (*_stack, gate_name))[0]
    memo[gate_name] = verdict
    return verdict


def _adjudicate(
    schema: _SchemaWithGates,
    gate: GateDef,
    loc: ChangeLocation,
    state: WorkflowState,
    params: dict,
    memo: dict[str, Verdict],
    stack: tuple[str, ...],
) -> tuple[Verdict, str | None]:
    verdict, matched = _adjudicate_base(schema, gate, loc, state, params, memo, stack)
    gate_name = stack[-1] if stack else ""
    upgraded, action = _apply_gate_decision(schema, gate_name, loc, verdict)
    if action is not None:
        return upgraded, f"{matched or 'default'}; human_decision:{action}"
    return verdict, matched


def _adjudicate_base(
    schema: _SchemaWithGates,
    gate: GateDef,
    loc: ChangeLocation,
    state: WorkflowState,
    params: dict,
    memo: dict[str, Verdict],
    stack: tuple[str, ...],
) -> tuple[Verdict, str | None]:
    # Step 1 — invalid_json: stop
    if gate.invalid_json == Verdict.STOP:
        for r in gate.reads:
            present, parse_error, _ = _load_doc(loc, r.path)
            if present and parse_error:
                return Verdict.STOP, "invalid_json"

    # Step 2 — build scope (gate: primary hoist + aliases; shared helper with engine)
    scope = build_evidence_scope(schema, loc, state, params, gate.reads, memo=memo, stack=stack)

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
    any_missing = any(not _resolve_path(loc, r.path).exists() for r in gate.reads)
    if any_missing and gate.missing_file_is:
        return gate.missing_file_is, "missing_file"

    # Step 6 — fail-closed default
    return gate.default, None


def is_codegen_hard_gate(gate_id: str) -> bool:
    """Codegen precondition gates cannot be overridden by human decision.

    Mirror of TS ``isCodegenHardGate``.
    """
    return gate_id.endswith("-codegen-precondition-gate")


def latest_valid_gate_decision(
    schema: _SchemaWithGates,
    gate_id: str,
    loc: ChangeLocation,
    *,
    events_dir: Path | None = None,
    checkpoint_ns: str | None = None,
    source_gate_attempt_id: str | None = None,
    source_gate_tree_id: str | None = None,
    require_source_epoch: bool = False,
) -> dict[str, object] | None:
    """Return the latest audited human or graph-resume decision for ``gate_id``.

    Mirror of TS ``latestValidGateDecision``: the decision must target this gate
    (for ``fixer-safety-gate``, a ``healing.safety`` accept_risk decision also
    counts), be an ``accept_risk``/``fix_and_proceed`` action with reason+who, and
    carry a ``review_file``/``review_sha256`` pointing at an audited gate read whose
    *current* hash still matches (the frozen evidence was not altered afterwards).
    When ``checkpoint_ns`` is supplied, only a complete v3 graph interrupt/resume
    chain on that namespace's ancestry is eligible; change-global legacy decisions
    are intentionally excluded. When ``require_source_epoch`` is true, graph
    overrides must also bind ``source_gate_attempt_id`` / ``source_gate_tree_id``.
    """
    events = _read_decision_events(events_dir or loc.path)
    if events is None:
        return None
    return _latest_valid_gate_decision_from_events(
        schema,
        gate_id,
        loc,
        events,
        checkpoint_ns=checkpoint_ns,
        source_gate_attempt_id=source_gate_attempt_id,
        source_gate_tree_id=source_gate_tree_id,
        require_source_epoch=require_source_epoch,
    )


def _read_decision_events(events_dir: Path) -> list[dict[str, object]] | None:
    try:
        return read_events_strict(events_dir)
    except (LedgerIntegrityError, OSError, UnicodeError):
        # A corrupt or unreadable authority cannot grant permission. Reading
        # this control plane must not mutate or repair append-only history.
        return None


def _latest_valid_gate_decision_from_events(
    schema: _SchemaWithGates,
    gate_id: str,
    loc: ChangeLocation,
    events: list[dict[str, object]],
    *,
    checkpoint_ns: str | None,
    source_gate_attempt_id: str | None = None,
    source_gate_tree_id: str | None = None,
    require_source_epoch: bool = False,
) -> dict[str, object] | None:
    gate = schema.gates.get(gate_id)
    if gate is None:
        return None
    audited = {r.path for r in gate.reads if is_audited_gate_read(r.path)}
    if not audited:
        return None
    latest_human: dict[str, object] | None = None
    # Legacy v1 adjudication has no graph namespace.  Graph task execution does,
    # and therefore accepts authority only from an exact interrupt/resume pair;
    # a change-global ``human_decision`` must never bleed into a fresh root run.
    if checkpoint_ns is None:
        for event in reversed(events):
            if event.get("source") != "decide" or event.get("type") != "human_decision":
                continue
            if _checkpoint_matches_gate(event.get("checkpoint"), gate_id):
                latest_human = event
                break
    latest_graph = _latest_graph_gate_decision(
        events,
        gate_id,
        current_checkpoint_ns=checkpoint_ns,
        source_gate_attempt_id=source_gate_attempt_id,
        source_gate_tree_id=source_gate_tree_id,
        require_source_epoch=require_source_epoch,
    )
    latest = max(
        (item for item in (latest_human, latest_graph) if item is not None),
        key=_event_sequence,
        default=None,
    )
    if latest is None:
        return None
    if latest.get("action") not in _GATE_DECISION_ACTIONS:
        return None
    reason = latest.get("reason")
    who = latest.get("who")
    if not (isinstance(reason, str) and reason.strip()):
        return None
    if not (isinstance(who, str) and who.strip()):
        return None
    if latest.get("source") == "graph":
        if not _decision_matches_source_epoch(
            latest,
            source_gate_attempt_id=source_gate_attempt_id,
            source_gate_tree_id=source_gate_tree_id,
            require_source_epoch=require_source_epoch,
        ):
            return None
        hashes = latest.get("audited_reads_sha256")
        if not isinstance(hashes, dict) or not audited.issubset(hashes):
            return None
        for rel in audited:
            expected = hashes.get(rel)
            if not isinstance(expected, str) or sha256_file(_resolve_path(loc, rel)) != expected:
                return None
        return latest
    if require_source_epoch:
        # Legacy human_decision events are not v5 gate-epoch overrides.
        return None
    review_file = latest.get("review_file")
    review_sha = latest.get("review_sha256")
    if not (isinstance(review_file, str) and isinstance(review_sha, str)):
        return None
    if review_file not in audited:
        return None
    # Mirror of the TS ``resolveDecisionSupport`` guard: ``healing.safety`` only
    # supports accept_risk (healing-safety consumer), so any other action recorded
    # there can never anchor the fixer-safety-gate.
    if (
        gate_id == "fixer-safety-gate"
        and latest.get("checkpoint") == "healing.safety"
        and latest.get("action") != "accept_risk"
    ):
        return None
    current = sha256_file(_resolve_path(loc, review_file))
    return latest if current == review_sha else None


def _event_sequence(event: Mapping[str, object]) -> int:
    value = event.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _latest_graph_gate_decision(
    events: list[dict[str, object]],
    gate_id: str,
    *,
    current_checkpoint_ns: str | None = None,
    source_gate_attempt_id: str | None = None,
    source_gate_tree_id: str | None = None,
    require_source_epoch: bool = False,
) -> dict[str, object] | None:
    """Resolve the latest resume group whose interrupt targeted ``gate_id``.

    A v3 resume is emitted once per ancestor invocation. Only the root copy owns
    the audited hashes, so the copies are treated as one decision keyed by
    ``interrupt_id`` rather than independently.
    """
    interruptions: dict[str, list[dict[str, object]]] = {}
    all_interruptions: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if event.get("source") != "graph" or event.get("type") != "graph_interrupted":
            continue
        interrupt_id = event.get("interrupt_id")
        if not isinstance(interrupt_id, str):
            continue
        all_interruptions.setdefault(interrupt_id, []).append(event)
        if _checkpoint_matches_gate(event.get("checkpoint"), gate_id):
            interruptions.setdefault(interrupt_id, []).append(event)

    candidates: list[dict[str, object]] = []
    for interrupt_id, matching_interruptions in interruptions.items():
        resumes = [
            event
            for event in events
            if event.get("source") == "graph"
            and event.get("type") == "graph_resumed"
            and event.get("interrupt_id") == interrupt_id
        ]
        if not resumes:
            continue
        if current_checkpoint_ns is None:
            candidate = _legacy_graph_resume_group(matching_interruptions[-1], resumes)
        else:
            candidate = _scoped_graph_resume_group(
                all_interruptions[interrupt_id],
                matching_interruptions,
                resumes,
                current_checkpoint_ns=current_checkpoint_ns,
            )
        if candidate is not None and _decision_matches_source_epoch(
            candidate,
            source_gate_attempt_id=source_gate_attempt_id,
            source_gate_tree_id=source_gate_tree_id,
            require_source_epoch=require_source_epoch,
        ):
            candidates.append(candidate)
    return max(candidates, key=_event_sequence, default=None)


def _legacy_graph_resume_group(
    interrupted: dict[str, object],
    resumes: list[dict[str, object]],
) -> dict[str, object]:
    """Preserve unscoped v1 decision behavior for non-graph callers."""
    latest_resume = max(resumes, key=_event_sequence)
    actions = interrupted.get("actions")
    if not isinstance(actions, list) or latest_resume.get("action") not in actions:
        return {**latest_resume, "action": "__invalid__"}
    if any(event.get("action") != latest_resume.get("action") for event in resumes):
        return {**latest_resume, "action": "__invalid__"}
    audited = next(
        (
            event.get("audited_reads_sha256")
            for event in resumes
            if isinstance(event.get("audited_reads_sha256"), dict) and event.get("audited_reads_sha256")
        ),
        {},
    )
    return {**latest_resume, "audited_reads_sha256": audited}


def _scoped_graph_resume_group(
    all_interruptions: list[dict[str, object]],
    matching_interruptions: list[dict[str, object]],
    resumes: list[dict[str, object]],
    *,
    current_checkpoint_ns: str,
) -> dict[str, object] | None:
    """Pair one v3 interrupt/resume group and scope it to a descendant task.

    A nested resume emits one copy per invocation layer.  Requiring that exact
    chain prevents a root copy from being fabricated for another run while
    still allowing a decision made in a review child to authorize its later
    codegen sibling through their shared ancestor.
    """
    full_namespaces = {event.get("checkpoint_ns") for event in matching_interruptions}
    if len(full_namespaces) != 1:
        return _invalid_graph_candidate(resumes)
    full_ns = next(iter(full_namespaces))
    if not isinstance(full_ns, str) or not full_ns.strip():
        return _invalid_graph_candidate(resumes)
    expected_layers = _resume_layers(full_ns)
    if expected_layers is None:
        return _invalid_graph_candidate(resumes)
    expected_nodes = _resume_nodes(full_ns)

    relevant_layers = [
        layer_ns
        for layer_ns in expected_layers.values()
        if _checkpoint_ns_is_ancestor(layer_ns, current_checkpoint_ns)
    ]
    if not relevant_layers:
        return None

    latest_resume = max(resumes, key=_event_sequence)
    resume_by_invocation: dict[str, dict[str, object]] = {}
    structurally_valid = len(resumes) == len(expected_layers)
    for event in resumes:
        invocation_id = event.get("invocation_id")
        checkpoint_ns = event.get("checkpoint_ns")
        if not isinstance(invocation_id, str) or invocation_id in resume_by_invocation:
            structurally_valid = False
            continue
        resume_by_invocation[invocation_id] = event
        if expected_layers.get(invocation_id) != checkpoint_ns:
            structurally_valid = False
        anchor = event.get("anchor")
        if not isinstance(anchor, dict):
            structurally_valid = False
        elif (
            anchor.get("invocation_id") != invocation_id
            or anchor.get("checkpoint_ns") != checkpoint_ns
            or anchor.get("interrupt_id") != event.get("interrupt_id")
            or not isinstance(anchor.get("node_id"), str)
            or (
                expected_nodes.get(invocation_id) is not None
                and anchor.get("node_id") != expected_nodes[invocation_id]
            )
        ):
            structurally_valid = False
    if set(resume_by_invocation) != set(expected_layers):
        structurally_valid = False
    parent_anchor_ref: str | None = None
    previous_resume_seq: int | None = None
    for invocation_id in expected_layers:
        resume = resume_by_invocation.get(invocation_id)
        if resume is None:
            structurally_valid = False
            continue
        resume_seq = _event_sequence(resume)
        if previous_resume_seq is not None and resume_seq <= previous_resume_seq:
            structurally_valid = False
        previous_resume_seq = resume_seq
        if resume.get("parent_anchor_ref") != parent_anchor_ref:
            structurally_valid = False
        anchor = resume.get("anchor")
        if isinstance(anchor, dict):
            encoded = json.dumps(
                anchor,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            parent_anchor_ref = hashlib.sha256(encoded).hexdigest()

    interruption_by_invocation: dict[str, dict[str, object]] = {}
    for event in all_interruptions:
        invocation_id = event.get("invocation_id")
        if not isinstance(invocation_id, str) or invocation_id not in expected_layers:
            structurally_valid = False
            continue
        previous = interruption_by_invocation.get(invocation_id)
        if previous is not None:
            # A crash/re-drive may re-bubble an identical immutable interrupt.
            # It adds no authority; conflicting duplicates make the pair invalid.
            if any(
                previous.get(field) != event.get(field)
                for field in (
                    "checkpoint_ns",
                    "interrupt_id",
                    "node_id",
                    "checkpoint",
                    "actions",
                    "audited_reads_sha256",
                    "anchor",
                )
            ):
                structurally_valid = False
            continue
        interruption_by_invocation[invocation_id] = event
        anchor = event.get("anchor")
        if not isinstance(anchor, dict):
            structurally_valid = False
        elif (
            anchor.get("invocation_id") != invocation_id
            or anchor.get("checkpoint_ns") != full_ns
            or anchor.get("interrupt_id") != event.get("interrupt_id")
            or anchor.get("node_id") != event.get("node_id")
        ):
            structurally_valid = False
    if set(interruption_by_invocation) != set(expected_layers):
        structurally_valid = False
    for invocation_id, resume in resume_by_invocation.items():
        interrupted = interruption_by_invocation.get(invocation_id)
        resume_anchor = resume.get("anchor")
        interrupted_anchor = interrupted.get("anchor") if interrupted is not None else None
        interrupted_node = interrupted.get("node_id") if interrupted is not None else None
        if (
            not isinstance(resume_anchor, dict)
            or not isinstance(interrupted_anchor, dict)
            or not isinstance(interrupted_node, str)
            or resume_anchor.get("node_id") != interrupted_node
            or interrupted_anchor.get("node_id") != interrupted_node
        ):
            structurally_valid = False

    interruption_checkpoints = {event.get("checkpoint") for event in all_interruptions}
    interruption_namespaces = {event.get("checkpoint_ns") for event in all_interruptions}
    interruption_actions = {_string_tuple(event.get("actions")) for event in all_interruptions}
    interruption_hashes = {
        _string_mapping_tuple(event.get("audited_reads_sha256")) for event in all_interruptions
    }
    if (
        len(interruption_checkpoints) != 1
        or len(interruption_namespaces) != 1
        or len(interruption_actions) != 1
        or len(interruption_hashes) != 1
        or None in interruption_actions
        or None in interruption_hashes
    ):
        structurally_valid = False

    if max(_event_sequence(event) for event in all_interruptions) >= min(
        _event_sequence(event) for event in resumes
    ):
        structurally_valid = False

    action = latest_resume.get("action")
    reason = latest_resume.get("reason")
    who = latest_resume.get("who")
    if any(
        event.get("action") != action or event.get("reason") != reason or event.get("who") != who
        for event in resumes
    ):
        structurally_valid = False
    declared_actions = next(iter(interruption_actions), None)
    if declared_actions is None or action not in declared_actions:
        structurally_valid = False

    interrupted_hashes = next(iter(interruption_hashes), None)
    root_invocation_id = next(iter(expected_layers))
    root_resume = resume_by_invocation.get(root_invocation_id)
    root_hashes = (
        _string_mapping_tuple(root_resume.get("audited_reads_sha256")) if root_resume is not None else None
    )
    if root_hashes is None or root_hashes != interrupted_hashes:
        structurally_valid = False
    for invocation_id, resume in resume_by_invocation.items():
        if invocation_id == root_invocation_id:
            continue
        if _string_mapping_tuple(resume.get("audited_reads_sha256")) != ():
            structurally_valid = False
    audited = dict(root_hashes or ())
    candidate = {
        **latest_resume,
        "seq": max(_event_sequence(event) for event in resumes),
        "audited_reads_sha256": audited,
        "accepted_checkpoint_ns": max(relevant_layers, key=lambda value: value.count("/")),
        "interrupt_checkpoint_ns": full_ns,
    }
    return candidate if structurally_valid else {**candidate, "action": "__invalid__"}


def _invalid_graph_candidate(resumes: list[dict[str, object]]) -> dict[str, object]:
    latest = max(resumes, key=_event_sequence)
    return {**latest, "action": "__invalid__"}


def _resume_layers(checkpoint_ns: str) -> dict[str, str] | None:
    parts = [part for part in checkpoint_ns.split("/") if part]
    if not parts or len(parts) % 2 == 0:
        return None
    invocation_ids = parts[::2]
    if len(invocation_ids) != len(set(invocation_ids)):
        return None
    return {parts[index]: "/".join(parts[: index + 1]) for index in range(0, len(parts), 2)}


def _resume_nodes(checkpoint_ns: str) -> dict[str, str | None]:
    parts = [part for part in checkpoint_ns.split("/") if part]
    return {
        parts[index]: parts[index + 1] if index + 1 < len(parts) else None
        for index in range(0, len(parts), 2)
    }


def _checkpoint_ns_is_ancestor(ancestor: str, descendant: str) -> bool:
    normalized_ancestor = ancestor.strip("/")
    normalized_descendant = descendant.strip("/")
    return normalized_descendant == normalized_ancestor or normalized_descendant.startswith(
        normalized_ancestor + "/"
    )


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _string_mapping_tuple(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        return None
    return tuple(sorted(value.items()))


def valid_ancestor_accept_risk_decisions(
    schema: _SchemaWithGates,
    loc: ChangeLocation,
    *,
    checkpoint_ns: str,
    events_dir: Path | None = None,
) -> tuple[AcceptedRiskDecision, ...]:
    """Project current, hash-anchored graph acceptances for an agent prompt."""
    events = _read_decision_events(events_dir or loc.path)
    if events is None:
        return ()
    decisions: list[AcceptedRiskDecision] = []
    for gate_id in sorted(schema.gates):
        event = _latest_valid_gate_decision_from_events(
            schema,
            gate_id,
            loc,
            events,
            checkpoint_ns=checkpoint_ns,
        )
        if event is None or event.get("source") != "graph" or event.get("action") != "accept_risk":
            continue
        interrupt_id = event.get("interrupt_id")
        accepted_ns = event.get("accepted_checkpoint_ns")
        reason = event.get("reason")
        who = event.get("who")
        if (
            not isinstance(interrupt_id, str)
            or not isinstance(accepted_ns, str)
            or not isinstance(reason, str)
            or not isinstance(who, str)
        ):
            continue
        decisions.append(
            AcceptedRiskDecision(
                gate_id=gate_id,
                interrupt_id=interrupt_id,
                checkpoint_ns=accepted_ns,
                reason=reason,
                who=who,
            )
        )
    return tuple(decisions)


def _apply_gate_decision(
    schema: _SchemaWithGates,
    gate_id: str,
    loc: ChangeLocation,
    base_verdict: Verdict,
    *,
    events_dir: Path | None = None,
    checkpoint_ns: str | None = None,
    source_gate_attempt_id: str | None = None,
    source_gate_tree_id: str | None = None,
    require_source_epoch: bool = False,
) -> tuple[Verdict, str | None]:
    """Apply a valid human decision to a ``needs_human_review`` gate verdict."""
    if base_verdict != Verdict.NEEDS_HUMAN_REVIEW or is_codegen_hard_gate(gate_id):
        return base_verdict, None
    decision = latest_valid_gate_decision(
        schema,
        gate_id,
        loc,
        events_dir=events_dir,
        checkpoint_ns=checkpoint_ns,
        source_gate_attempt_id=source_gate_attempt_id,
        source_gate_tree_id=source_gate_tree_id,
        require_source_epoch=require_source_epoch,
    )
    if decision is None:
        return base_verdict, None
    action = decision.get("action")
    if action == "accept_risk":
        return Verdict.PASS, "accept_risk"
    if action == "fix_and_proceed":
        return Verdict.NEEDS_FIX, "fix_and_proceed"
    return base_verdict, None


def check_gate(
    schema: _SchemaWithGates,
    gate_name: str,
    loc: ChangeLocation,
    state: WorkflowState,
    params: dict,
) -> GateVerdict:
    gate = schema.gates.get(gate_name)
    if gate is None:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason="unknown gate")
    memo: dict[str, Verdict] = {}
    try:
        verdict, matched = _adjudicate(schema, gate, loc, state, params, memo, (gate_name,))
    except GateCycleError as exc:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason=str(exc))
    return GateVerdict(gate=gate_name, verdict=verdict, matched_rule=matched)


# ---------------------------------------------------------------------------
# graph artifact view 上的冻结 gate 求值（schema v2）
#
# ``check_gate_in_view`` 与 v1 ``check_gate`` 保持同一份裁决语义（invalid_json →
# 有序规则 first-true-wins → missing_field_is → missing_file_is → fail-closed
# default），但所有输入都来自显式 ``GateEvaluationContext``：三个逻辑 root 指向
# task 私有 workspace（attached gate）或 committed graph workspace（builtin
# gate），state/gate()/node() 结局来自冻结映射而非 v1 ledger/engine 递归。
# v1 ``check_gate`` 路径保持原实现不变，直到 Task 15 切换。


class GateError(AaError):
    """artifact view 上的 gate 求值失败（未知 gate 等）。"""


@dataclass(frozen=True)
class GateEvaluationContext:
    """一次冻结 gate 求值的显式 artifact view。"""

    project_root: Path
    repo_root: Path
    change_dir: Path
    change_id: str
    params: Mapping[str, object]
    state_values: Mapping[str, object]
    node_results: Mapping[str, object]
    artifact_overrides: Mapping[str, object] = field(default_factory=dict)
    # Coordinator events are intentionally excluded from frozen task trees.
    # Gate evidence still comes from change_dir; only decisions come from here.
    audit_events_dir: Path | None = None
    # Namespace of the task consuming the gate.  Graph resume authority is
    # scoped to this namespace's ancestors; ``None`` preserves v1 callers that
    # have no graph checkpoint identity.
    checkpoint_ns: str | None = None
    # v5 decision epochs bind the current committed tree (and resolved attempt).
    committed_tree_id: str | None = None
    event_schema_version: int | None = None
    invocation_id: str | None = None


class FrozenGateReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    gate_id: str
    verdict: Verdict
    matched_rule: str | None
    reason: str
    reads_sha256: dict[str, str]
    details: Mapping[str, Any] | None = None


def expand_gate_read_template(template: str, *, params: Mapping[str, object]) -> str:
    """Expand allowlisted scalar `${params.name}` path segments fail-closed."""
    token = re.compile(r"\$\{params\.([A-Za-z_][A-Za-z0-9_]*)\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = params.get(name)
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise GateError(f"unsafe or missing gate path param: {name}")
        segment = str(value)
        digest_ok = re.fullmatch(r"sha256:[0-9a-f]{64}", segment) is not None
        ordinary_ok = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", segment) is not None
        if not (digest_ok or ordinary_ok) or segment in {".", ".."}:
            raise GateError(f"unsafe gate path param: {name}")
        return segment

    expanded = token.sub(replace, template)
    if "${" in expanded or "%2f" in expanded.lower() or "%5c" in expanded.lower():
        raise GateError("unresolved or encoded traversal in gate path")
    return expanded


def resolve_view_path(
    context: GateEvaluationContext,
    rel: str,
    *,
    params: Mapping[str, object] | None = None,
) -> Path:
    """``resolve_change_path`` 的 view 版本：``repo:`` → repo root，``qa/`` → project root，其余 → change dir。"""
    normalized = expand_gate_read_template(
        rel.replace("<change-id>", context.change_id), params=params or context.params
    )
    if normalized.startswith("repo:"):
        return context.repo_root / normalized[len("repo:") :]
    if normalized.startswith("qa/"):
        return context.project_root / normalized
    return context.change_dir / normalized


def check_gate_in_view(
    gates: Mapping[str, GateDef],
    gate_id: str,
    context: GateEvaluationContext,
) -> FrozenGateReport:
    gate = gates.get(gate_id)
    if gate is None:
        raise GateError(f"unknown gate: {gate_id}")
    verdict, matched_rule, reason, reads_sha256, details = _evaluate_gate_def(
        gate, context, gates=gates, stack=(gate_id,), memo={}
    )
    return FrozenGateReport(
        gate_id=gate_id,
        verdict=verdict,
        matched_rule=matched_rule,
        reason=reason,
        reads_sha256=reads_sha256,
        details=details,
    )


def _load_view_doc(context: GateEvaluationContext, rel: str) -> tuple[bool, bool, object]:
    """``_load_doc`` 的 view 版本：returns (present, parse_error, value)。"""
    if rel in context.artifact_overrides:
        return True, False, context.artifact_overrides[rel]
    path = resolve_view_path(context, rel)
    if not path.exists():
        return False, False, None
    try:
        text = path.read_text(encoding="utf-8")
        if rel.endswith((".yaml", ".yml")):
            return True, False, yaml.safe_load(text)
        return True, False, json.loads(text)
    except (json.JSONDecodeError, yaml.YAMLError, ValueError):
        return True, True, None


def _view_gate_verdict(
    context: GateEvaluationContext,
    gate_id: str,
    *,
    gates: Mapping[str, GateDef],
    stack: tuple[str, ...],
    memo: dict[str, str],
) -> str:
    """解析 ``gate()``：优先冻结 node 结局，否则在同一 view 上重新裁决。

    本图内已有节点裁决过该 gate 时，那份 ``gate_report`` 是冻结证据，直接采用。
    否则必须就地重算——**子图内**裁决的 gate 在父图 view 里没有任何 node 结局
    （例如 ``*-plan-cycle.review`` 之于 ``*-branch``），若一律返回 stop，跨子图
    引用就会静默变成永久阻塞。重算与 v1 ``resolve_gate_verdict`` 同语义：同一
    view、memo 去重、成环 fail closed。
    """
    for result in context.node_results.values():
        if not isinstance(result, dict):
            continue
        report = result.get("gate")
        if isinstance(report, dict) and report.get("gate_id") == gate_id:
            verdict = report.get("verdict")
            if isinstance(verdict, str):
                try:
                    frozen_verdict = Verdict(verdict)
                except ValueError:
                    return Verdict.STOP.value
                location = ChangeLocation(
                    project_root=context.project_root,
                    change_id=context.change_id,
                    path=context.change_dir,
                    source="changes",
                )
                upgraded, action = _apply_gate_decision(
                    _ViewGateSchema(gates=gates),
                    gate_id,
                    location,
                    frozen_verdict,
                    events_dir=context.audit_events_dir,
                    checkpoint_ns=context.checkpoint_ns,
                )
                return (frozen_verdict if action is None else upgraded).value
    if gate_id in memo:
        return memo[gate_id]
    referenced = gates.get(gate_id)
    if referenced is None or gate_id in stack:
        return Verdict.STOP.value
    resolved = _evaluate_gate_def(referenced, context, gates=gates, stack=(*stack, gate_id), memo=memo)[0]
    memo[gate_id] = resolved.value
    return resolved.value


def _view_scope(
    gate: GateDef,
    context: GateEvaluationContext,
    *,
    gates: Mapping[str, GateDef],
    stack: tuple[str, ...],
    memo: dict[str, str],
) -> Scope:
    """view 版 gate 作用域：primary hoist + aliases + params/state + 冻结结局解析器。"""
    alias_docs: dict[str, object] = {}
    for entry in gate.reads:
        _, _, val = _load_view_doc(context, entry.path)
        alias_docs[entry.alias] = val
    primary_val = alias_docs.get(gate.reads[0].alias) if gate.reads else None
    hoisted = primary_val if isinstance(primary_val, dict) else {}
    scope_vars: dict[str, object] = {
        **hoisted,
        **alias_docs,
        "params": dict(context.params),
        "state": dict(context.state_values),
        "policy": load_policy(context.project_root).model_dump(mode="json"),
    }

    def file_exists(rel: str) -> bool:
        return resolve_view_path(context, rel).exists()

    def gate_verdict(gid: str) -> str:
        return _view_gate_verdict(context, gid, gates=gates, stack=stack, memo=memo)

    def node_result(node_id: str) -> object:
        result = context.node_results.get(node_id)
        return result if isinstance(result, dict) else {}

    def resolve_plan_assurance_state(
        checks: object, review: object, data_knowledge: object, layer: object
    ) -> str:
        return plan_assurance_state(
            checks,
            review,
            data_knowledge,
            layer,
            change_id=context.change_id,
        )

    return Scope(
        scope_vars,
        file_exists=file_exists,
        gate_verdict=gate_verdict,
        node_result=node_result,
        capabilities_present=check_capabilities_present,
        plan_assurance_state=resolve_plan_assurance_state,
    )


def _gate_details(gate: GateDef, scope: Scope) -> dict[str, Any] | None:
    review_doc: dict | None = None
    dk_doc: dict | None = None
    for entry in gate.reads:
        value = scope.lookup(entry.alias)
        if entry.path == "repo:.aa/data-knowledge.yaml" and isinstance(value, dict):
            dk_doc = value
        elif entry.path.endswith(".json") and "plan-review" in entry.path and isinstance(value, dict):
            review_doc = value
    if review_doc is None or dk_doc is None:
        return None
    return {"missing_capabilities": compute_missing_capabilities(review_doc, dk_doc)}


def _audited_reads_sha256(gate: GateDef, context: GateEvaluationContext) -> dict[str, str]:
    """冻结每个 audited gate read 的当前内容 hash；缺失文件无法 hash，按缺失省略。"""
    hashes: dict[str, str] = {}
    for entry in gate.reads:
        if not is_audited_gate_read(entry.path):
            continue
        digest = sha256_file(resolve_view_path(context, entry.path))
        if digest is not None:
            hashes[entry.path] = digest
    return hashes


def _current_gate_attempt_id(
    events: list[dict[str, object]],
    *,
    gate_id: str,
    invocation_id: str | None,
) -> str | None:
    latest: str | None = None
    for event in events:
        if invocation_id is not None and event.get("invocation_id") != invocation_id:
            continue
        if event.get("type") != "task_attempt_succeeded":
            continue
        report = event.get("gate_report")
        attempt_id = event.get("attempt_id")
        if isinstance(report, Mapping) and report.get("gate_id") == gate_id and isinstance(attempt_id, str):
            latest = attempt_id
    return latest


def _evaluate_gate_def(
    gate: GateDef,
    context: GateEvaluationContext,
    *,
    gates: Mapping[str, GateDef],
    stack: tuple[str, ...],
    memo: dict[str, str],
) -> tuple[Verdict, str | None, str, dict[str, str], dict[str, Any] | None]:
    """Evaluate a gate in the frozen view, including anchored human decisions."""
    verdict, matched, reason, reads_sha256, details = _evaluate_gate_def_base(
        gate,
        context,
        gates=gates,
        stack=stack,
        memo=memo,
    )
    gate_id = stack[-1] if stack else ""
    schema = _ViewGateSchema(gates=gates)
    location = ChangeLocation(
        project_root=context.project_root,
        change_id=context.change_id,
        path=context.change_dir,
        source="changes",
    )
    require_source_epoch = (context.event_schema_version or 0) >= 5
    source_attempt: str | None = None
    source_tree = context.committed_tree_id
    if require_source_epoch:
        events = read_events_strict(context.audit_events_dir or context.change_dir)
        source_attempt = _current_gate_attempt_id(
            events, gate_id=gate_id, invocation_id=context.invocation_id
        )
    upgraded, action = _apply_gate_decision(
        schema,
        gate_id,
        location,
        verdict,
        events_dir=context.audit_events_dir,
        checkpoint_ns=context.checkpoint_ns,
        source_gate_attempt_id=source_attempt,
        source_gate_tree_id=source_tree,
        require_source_epoch=require_source_epoch,
    )
    final_verdict = verdict if action is None else upgraded
    final_details = _details_with_stop_cause(
        gate,
        context,
        gates=gates,
        stack=stack,
        memo=memo,
        verdict=final_verdict,
        details=details,
    )
    if action is None:
        return verdict, matched, reason, reads_sha256, final_details
    decision_match = f"{matched or 'default'}; human_decision:{action}"
    return upgraded, decision_match, f"{reason}; human decision {action}", reads_sha256, final_details


@dataclass(frozen=True)
class _ViewGateSchema:
    gates: Mapping[str, GateDef]


def _details_with_stop_cause(
    gate: GateDef,
    context: GateEvaluationContext,
    *,
    gates: Mapping[str, GateDef],
    stack: tuple[str, ...],
    memo: dict[str, str],
    verdict: Verdict,
    details: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if verdict != Verdict.STOP or not gate.causes:
        return details
    scope = _view_scope(gate, context, gates=gates, stack=stack, memo=memo)
    cause = next(
        (
            name
            for name, expression in gate.causes.items()
            if evaluate(parse_expression(expression), scope) is True
        ),
        f"{gate.id}.unknown",
    )
    merged = dict(details or {})
    merged["cause"] = cause
    return merged


_METRICS_SUFFICIENCY_GATE_ID = "metrics-sufficiency-gate"
_METRICS_VERDICT_MAP = {
    "pass": Verdict.PASS,
    "needs_human": Verdict.NEEDS_HUMAN_REVIEW,
    "reject": Verdict.REJECT,
    "stop": Verdict.STOP,
    "skipped": Verdict.SKIP,
}


def _evaluate_metrics_sufficiency_gate(
    gate: GateDef,
    context: GateEvaluationContext,
) -> tuple[Verdict, str | None, str, dict[str, str], dict[str, Any] | None]:
    """Adjudicate ``inspect/metrics.json`` via ``evaluate_metrics_sufficiency``.

    Floors / cadence / ``on_insufficient`` are not re-expressed as DSL — the
    Task-2 Python consumer is the single truth table (anti-tautology).
    """
    from pydantic import ValidationError

    from assurance_agent.artifacts.models.metrics import MetricsDocument
    from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency

    hashes = _audited_reads_sha256(gate, context)
    if not gate.reads:
        return gate.default, None, "metrics gate has no reads", hashes, None

    path = gate.reads[0].path
    present, parse_error, raw = _load_view_doc(context, path)
    if not present:
        verdict = gate.missing_file_is or gate.default
        return verdict, "missing_file", "gate read file is missing", hashes, None
    if parse_error or not isinstance(raw, dict):
        verdict = gate.invalid_json or gate.default
        return verdict, "invalid_json", "gate read contains invalid JSON", hashes, None
    try:
        document = MetricsDocument.model_validate(raw)
    except ValidationError:
        verdict = gate.missing_field_is or gate.default
        return (
            verdict,
            "missing_field",
            "metrics document failed schema validation",
            hashes,
            None,
        )

    decision = evaluate_metrics_sufficiency(
        document,
        load_policy(context.project_root).evidence_sufficiency,
    )
    mapped = _METRICS_VERDICT_MAP.get(decision.verdict, gate.default)
    details: dict[str, Any] = {
        "evaluator": "assurance_agent.evidence.metrics_sufficiency::evaluate_metrics_sufficiency",
        "decision": decision.verdict,
        "mutation_budget_seconds": decision.mutation_budget_seconds,
        "shortboards": [board.model_dump(mode="json") for board in decision.shortboards],
    }
    return (
        mapped,
        "evaluate_metrics_sufficiency",
        f"metrics sufficiency verdict {decision.verdict}",
        hashes,
        details,
    )


def _evaluate_gate_def_base(
    gate: GateDef,
    context: GateEvaluationContext,
    *,
    gates: Mapping[str, GateDef],
    stack: tuple[str, ...],
    memo: dict[str, str],
) -> tuple[Verdict, str | None, str, dict[str, str], dict[str, Any] | None]:
    """在显式 view 上求值单个 GateDef 的基础规则，不含人工决策升级。

    规则求值顺序与 v1 ``_adjudicate_base`` 逐步对齐（声明序 first-true-wins）；
    每个返回路径都冻结当前 audited read hash。
    """
    # Step 1 — invalid_json: stop
    if gate.invalid_json == Verdict.STOP:
        for entry in gate.reads:
            present, parse_error, _ = _load_view_doc(context, entry.path)
            if present and parse_error:
                return (
                    Verdict.STOP,
                    "invalid_json",
                    "gate read contains invalid JSON",
                    _audited_reads_sha256(gate, context),
                    None,
                )

    # metrics-sufficiency-gate: Python evaluator, not DSL rules.
    if gate.id == _METRICS_SUFFICIENCY_GATE_ID:
        return _evaluate_metrics_sufficiency_gate(gate, context)

    # Step 2 — scope（gate: primary hoist + aliases + params/state + 冻结结局）
    scope = _view_scope(gate, context, gates=gates, stack=stack, memo=memo)
    details = _gate_details(gate, scope)

    # Step 3 — rules in DECLARATION order, first-true-wins（与 v1 相同）
    saw_missing = False
    for rule in gate.rules:
        result = evaluate(parse_expression(rule.expr), scope)
        if result is True:
            matched = f"{rule.field}: {rule.expr}"
            return (
                rule.verdict,
                matched,
                f"matched rule {rule.field}: {rule.expr}",
                _audited_reads_sha256(gate, context),
                details,
            )
        if result is MISSING:
            saw_missing = True

    # Step 4 — missing_field_is
    if saw_missing and gate.missing_field_is:
        return (
            gate.missing_field_is,
            "missing_field",
            "gate read is missing a referenced field",
            _audited_reads_sha256(gate, context),
            details,
        )

    # Step 5 — missing_file_is
    any_missing = any(not resolve_view_path(context, entry.path).exists() for entry in gate.reads)
    if any_missing and gate.missing_file_is:
        return (
            gate.missing_file_is,
            "missing_file",
            "gate read file is missing",
            _audited_reads_sha256(gate, context),
            details,
        )

    # Step 6 — fail-closed default
    return gate.default, None, "fail-closed default", _audited_reads_sha256(gate, context), details
