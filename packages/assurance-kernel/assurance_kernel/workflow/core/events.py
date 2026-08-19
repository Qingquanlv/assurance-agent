"""events.jsonl append-only 写入：遥测型 best-effort + 审计型 strict（对齐源版 events.ts）。

M3 只提供 append 原语 + 读取；事务性写边界（先事件后 state、失败回滚）在 M6 progression
以文件快照 + 幂等标记实现。core 内不 import state，避免层内环。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.graph_events import GRAPH_EVENT_ADAPTER, GraphEvent

EVENTS_RELPATH = "events.jsonl"
_LEDGER_ENVELOPE_KEYS = frozenset({"seq", "ts"})


class _AuditEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HumanDecisionEvent(_AuditEventBase):
    source: Literal["decide"] = "decide"
    type: Literal["human_decision"] = "human_decision"
    checkpoint: str
    action: Literal["fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"]
    reason: str
    who: str
    evidence_file: str | None = None
    evidence_sha256: str | None = None
    review_file: str | None = None
    review_sha256: str | None = None


class DispatchSignedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["dispatch_signed"] = "dispatch_signed"
    phase: str
    kind: Literal["dispatch_phase", "heal"]
    target: Literal["api", "e2e"] | None = None
    attempt_id: str
    state_guard: str
    dispatched_at: int


class PhaseOutcomeCommittedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["phase_outcome_committed"] = "phase_outcome_committed"
    phase: str
    attempt_id: str
    gate_report: dict[str, object] | None


class HealingAttemptAllocatedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["healing_attempt_allocated"] = "healing_attempt_allocated"
    episode_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str


class HealRecordApplyEvent(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["heal_record_apply"] = "heal_record_apply"
    target: Literal["api", "e2e"]
    proposal_sha256: str
    source_batch_id: str
    attempt_key: str
    summary_sha256: str | None
    files_modified: list[str]


class HealTransitionEvent(_AuditEventBase):
    source: Literal["status"] = "status"
    type: Literal["heal_transition"] = "heal_transition"
    from_: str = Field(alias="from")
    to: str
    source_batch_id: str | None = None
    proposal_sha256: str | None = None
    attempt_key: str | None = None


class HealingEntryBaselinePinnedEvent(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["healing_entry_baseline_pinned"] = "healing_entry_baseline_pinned"
    artifact_file: Literal["healing/entry-baseline.json"]
    artifact_sha256: str
    entry_batch_id: str
    episode_id: str


class HealingAttemptAllocatedV2Event(_AuditEventBase):
    """Combined baseline+allocation domain event for D14 healing outbox recovery."""

    source: Literal["progression"] = "progression"
    type: Literal["healing_attempt_allocated_v2"] = "healing_attempt_allocated_v2"
    episode_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str
    entry_batch_id: str
    baseline_sha256: str
    baseline_embedded: bool


class FixerProposalApprovedEvent(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["fixer_proposal_approved"] = "fixer_proposal_approved"
    approval_id: str
    root_invocation_id: str
    interrupt_task_id: str
    source_gate_attempt_id: str
    source_tree_id: str
    proposal_sha256: str
    fixer_authority_sha256: str
    entry_baseline_sha256: str
    policy_sha256: str
    targets: list[Literal["api", "e2e"]]
    paths: list[str]
    target_tree_id: str


class HealRecordApplyV2Event(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["heal_record_apply_v2"] = "heal_record_apply_v2"
    record_key: str
    root_invocation_id: str
    record_task_id: str
    fixer_attempt_id: str
    target: Literal["api", "e2e"]
    entry_batch_id: str
    intent_sha256: str
    write_set_id: str
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[str]
    claimed_modified_paths: list[str]
    safety_payload_sha256: str


class GateVerdictEvent(_AuditEventBase):
    """Durable gate adjudication with audited-read hashes for tamper detection."""

    source: Literal["gate"] = "gate"
    type: Literal["gate_verdict"] = "gate_verdict"
    phase: str | None = None
    gate: str
    verdict: str
    blocks: int | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    reads_sha256: dict[str, str] | None = None
    matched_rule: str | None = None
    reason: str | None = None


class FailureReclassifiedEvent(_AuditEventBase):
    """Ledger proof that a failure category change was intentional."""

    source: Literal["report"] = "report"
    type: Literal["failure_reclassified"] = "failure_reclassified"
    failure: str
    from_: str = Field(alias="from")
    to: str
    evidence: str


AuditEvent = Annotated[
    HumanDecisionEvent
    | DispatchSignedEvent
    | PhaseOutcomeCommittedEvent
    | HealingAttemptAllocatedEvent
    | HealingAttemptAllocatedV2Event
    | HealRecordApplyEvent
    | HealRecordApplyV2Event
    | HealTransitionEvent
    | HealingEntryBaselinePinnedEvent
    | FixerProposalApprovedEvent
    | GateVerdictEvent
    | FailureReclassifiedEvent
    | GraphEvent,
    Field(discriminator="type"),
]
_AUDIT_ADAPTER = TypeAdapter(AuditEvent)


class EventWriteError(AaError):
    """strict 审计事件写入失败。"""


class LedgerIntegrityError(AaError):
    """strict ledger 读取失败：坏 JSON、非 dict 行、seq 缺口或非法 graph 事件。"""


def _events_file(change_dir: Path) -> Path:
    return change_dir / EVENTS_RELPATH


def event_seq(event: Mapping[str, object]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


class Ledger:
    """Sequence-aware query interface over a Change's events.jsonl."""

    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir

    def all(self) -> list[dict[str, object]]:
        return read_events(self._change_dir)

    def filter(
        self,
        *,
        type: str | None = None,
        after_seq: int | None = None,
        **attrs: object,
    ) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for event in self.all():
            if type is not None and event.get("type") != type:
                continue
            if after_seq is not None and event_seq(event) <= after_seq:
                continue
            if any(event.get(key) != value for key, value in attrs.items()):
                continue
            out.append(event)
        return out

    def latest(
        self,
        *,
        type: str | None = None,
        after_seq: int | None = None,
        **attrs: object,
    ) -> dict[str, object] | None:
        matches = self.filter(type=type, after_seq=after_seq, **attrs)
        if not matches:
            return None
        return max(matches, key=event_seq)


def read_events(change_dir: Path) -> list[dict[str, object]]:
    file = _events_file(change_dir)
    if not file.exists():
        return []
    out: list[dict[str, object]] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue  # 容错：跳过坏行（对齐源版 readEvents）
        if isinstance(value, dict):
            out.append(value)
    return out


def read_events_raw(change_dir: Path) -> list[dict[str, object]]:
    """Fail-closed 读取 ledger 行：JSON + seq 连续；**不**校验 graph payload。"""
    file = _events_file(change_dir)
    if not file.exists():
        return []
    out: list[dict[str, object]] = []
    expected_seq = 0
    for line_no, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError(f"{file} line {line_no}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise LedgerIntegrityError(f"{file} line {line_no}: event is not a JSON object")
        expected_seq += 1
        seq = value.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise LedgerIntegrityError(f"{file} line {line_no}: expected seq {expected_seq}, got {seq!r}")
        out.append(value)
    return out


def read_events_strict(change_dir: Path) -> list[dict[str, object]]:
    """Fail-closed 读取：seq 校验 → v1→v2 migrate → graph payload 校验。"""
    from assurance_kernel.workflow.core.migrate_events import migrate_events_for_fold

    events = migrate_events_for_fold(read_events_raw(change_dir))
    file = _events_file(change_dir)
    for line_no, value in enumerate(events, start=1):
        if value.get("source") != "graph":
            continue
        payload = {k: v for k, v in value.items() if k not in _LEDGER_ENVELOPE_KEYS}
        try:
            GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise LedgerIntegrityError(f"{file} line {line_no}: invalid graph event: {exc}") from exc
    return events


def next_seq(change_dir: Path) -> int:
    seqs: list[int] = []
    for e in read_events(change_dir):
        seq = e.get("seq")
        if isinstance(seq, int):
            seqs.append(seq)
    return (max(seqs) + 1) if seqs else 1


def _append(change_dir: Path, event: Mapping[str, object]) -> None:
    if not change_dir.exists():
        raise EventWriteError(f"change directory does not exist: {change_dir}")
    record = {
        "seq": next_seq(change_dir),
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with _events_file(change_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_event_strict(
    change_dir: Path,
    event: AuditEvent | Mapping[str, object],
) -> None:
    try:
        validated = _AUDIT_ADAPTER.validate_python(event)
        _append(change_dir, validated.model_dump(mode="json", by_alias=True, exclude_none=True))
    except (EventWriteError, OSError, TypeError, ValueError, ValidationError) as exc:
        raise EventWriteError(str(exc)) from exc


def append_event_best_effort(change_dir: Path, event: Mapping[str, object]) -> None:
    try:
        _append(change_dir, event)
    except Exception as exc:  # telemetry 永不改变调用者结果；不捕获 BaseException
        print(f"warning: events.jsonl skipped: {exc}", file=sys.stderr)
