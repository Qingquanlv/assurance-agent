"""Read-only Workflow history adapters for Retro window resolution and evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.paths import existing_with_alias
from assurance_agent.change_location import (
    ChangeNotFoundError,
    archive_root,
    changes_root,
    resolve_change,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.healing.projection import project_healing_episode_from_events


def _list_dir_names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


if TYPE_CHECKING:
    from assurance_agent.retro.window import ResolvedRetroWindow

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_TERMINAL_TYPES = frozenset({"graph_completed", "graph_stopped", "graph_failed"})
_CHANGE_TERMINAL_ENTRYPOINTS = frozenset({"full", "execute"})
_PUSHBACK_VERDICTS = frozenset({"needs_fix", "fail", "blocked", "stop"})


class WorkflowHistoryIntegrityError(AaError):
    """Raised when a Change workflow ledger is corrupt or unreadable."""


def _batch_gap_reason(change_id: str, status: str, reason_code: str) -> str:
    return f"batch_member_evidence_gap:{change_id}:{status}:workflow:{reason_code}"


class TerminalChangeRef(BaseModel):
    """One terminal Change ordered by authoritative ledger event time."""

    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    terminal_ts: str = Field(min_length=1)
    terminal_event_type: str = "graph_completed"
    head_seq: int | None = None
    ledger_sha256: str = ""


class GateVerdictRecord(BaseModel):
    model_config = _FROZEN

    change_id: str
    seq: int
    ts: str
    gate: str
    verdict: str
    reason: str | None = None
    cause: str | None = None
    evidence_id: str


class HealingAllocationRecord(BaseModel):
    model_config = _FROZEN

    change_id: str
    seq: int
    ts: str
    operation_id: str
    evidence_id: str


class HealingApplyRecord(BaseModel):
    model_config = _FROZEN

    change_id: str
    target: str
    applied: bool
    evidence_id: str
    seq: int | None = Field(default=None, ge=1)
    ts: str | None = None
    operation_id: str | None = None


class SkillLoadedFalseRecord(BaseModel):
    model_config = _FROZEN

    change_id: str
    phase: str
    expected_skill: str | None = None
    evidence_id: str


class WorkflowTaskFailureRecord(BaseModel):
    """Final task failure pinned to its immutable attempt event."""

    model_config = _FROZEN

    change_id: str
    task_id: str | None = None
    attempt_id: str | None = None
    node_id: str
    ts: str | None = None
    error_kind: ErrorKind
    message: str
    recovered: bool
    evidence_id: str


class WorkflowEvidenceSlice(BaseModel):
    """Digest-pinned Workflow evidence for one Retro window."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_ids: tuple[str, ...]
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    gate_verdicts: tuple[GateVerdictRecord, ...] = ()
    healing_allocations: tuple[HealingAllocationRecord, ...] = ()
    healing_applies: tuple[HealingApplyRecord, ...] = ()
    skill_loaded_false: tuple[SkillLoadedFalseRecord, ...] = ()
    task_failures: tuple[WorkflowTaskFailureRecord, ...] = ()

    def resolvable_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for source in self.sources:
            ids.update(source.evidence_ids)
        ids.update(record.evidence_id for record in self.gate_verdicts)
        ids.update(record.evidence_id for record in self.healing_allocations)
        ids.update(record.evidence_id for record in self.healing_applies)
        ids.update(record.evidence_id for record in self.skill_loaded_false)
        ids.update(record.evidence_id for record in self.task_failures)
        return frozenset(ids)


@runtime_checkable
class WorkflowHistoryReader(Protocol):
    def list_terminal_changes(
        self,
        change_ids: tuple[str, ...] | None = None,
        *,
        tolerate_member_errors: bool = False,
    ) -> tuple[TerminalChangeRef, ...]: ...

    def discover_change_ids(self) -> frozenset[str]: ...

    def read_window(self, window: ResolvedRetroWindow) -> WorkflowEvidenceSlice: ...


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _event_seq(event: Mapping[str, object]) -> int | None:
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return None


def _event_evidence_id(change_id: str, event: Mapping[str, object]) -> str | None:
    seq = _event_seq(event)
    return f"{change_id}#seq{seq}" if seq is not None else None


def _gate_cause(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    details = value.get("details")
    if not isinstance(details, Mapping):
        return None
    cause = details.get("cause")
    return cause if isinstance(cause, str) and cause else None


def _read_ledger_strict(change_id: str, change_dir: Path) -> tuple[bytes, list[dict[str, object]]]:
    """Strict envelope read; never soft-skips malformed JSONL lines."""
    events_path = change_dir / "events.jsonl"
    if not events_path.is_file():
        return b"", []
    try:
        data = events_path.read_bytes()
    except OSError as exc:
        raise WorkflowHistoryIntegrityError(f"workflow_ledger_corrupt:{change_id}: {exc}") from exc
    if not data:
        return data, []
    try:
        events = read_events_strict(change_dir)
    except (LedgerIntegrityError, UnicodeDecodeError) as exc:
        raise WorkflowHistoryIntegrityError(f"workflow_ledger_corrupt:{change_id}: {exc}") from exc
    return data, events


def _terminal_from_events(
    change_id: str,
    events: Sequence[Mapping[str, object]],
    ledger_sha256: str,
    *,
    archived: bool,
) -> TerminalChangeRef | None:
    root_change_invocations = {
        invocation_id
        for event in events
        for invocation_id in (event.get("invocation_id"),)
        if event.get("type") == "graph_invocation_started"
        and isinstance(invocation_id, str)
        and not event.get("parent_invocation_id")
        and event.get("entrypoint") in _CHANGE_TERMINAL_ENTRYPOINTS
    }
    terminal_event: Mapping[str, object] | None = None
    for event in events:
        if event.get("type") in _TERMINAL_TYPES and event.get("invocation_id") in root_change_invocations:
            terminal_event = event
    if terminal_event is not None:
        ts = terminal_event.get("ts")
        if not isinstance(ts, str) or not ts:
            return None
        seq = terminal_event.get("seq")
        return TerminalChangeRef(
            change_id=change_id,
            terminal_ts=ts,
            terminal_event_type=str(terminal_event.get("type")),
            head_seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            ledger_sha256=ledger_sha256,
        )
    if not archived or not events:
        return None
    # Archived snapshot without an explicit terminal graph event: use the
    # latest authoritative event timestamp, never directory mtime.
    last = events[-1]
    ts = last.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    seq = last.get("seq")
    return TerminalChangeRef(
        change_id=change_id,
        terminal_ts=ts,
        terminal_event_type="archived",
        head_seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        ledger_sha256=ledger_sha256,
    )


def _extract_from_change(
    change_id: str,
    change_dir: Path,
) -> tuple[
    RetroSourceDescriptor | None,
    list[GateVerdictRecord],
    list[HealingAllocationRecord],
    list[HealingApplyRecord],
    list[SkillLoadedFalseRecord],
    list[WorkflowTaskFailureRecord],
    str | None,
]:
    events_path = change_dir / "events.jsonl"
    if not events_path.is_file():
        return None, [], [], [], [], [], f"workflow_source_missing:{change_id}"
    data, events = _read_ledger_strict(change_id, change_dir)

    ledger_sha = _sha256_bytes(data)
    gate_verdicts: list[GateVerdictRecord] = []
    healing_allocations: list[HealingAllocationRecord] = []
    evidence_ids: list[str] = []
    task_starts: dict[str, Mapping[str, object]] = {}
    task_settlements: dict[str, Mapping[str, object]] = {}
    recovery_routes: dict[str, Mapping[str, object]] = {}
    for event in events:
        eid = _event_evidence_id(change_id, event)
        event_type = event.get("type")
        task_id = event.get("task_id")
        if event_type == "task_attempt_started" and isinstance(task_id, str):
            node_id = event.get("node_id")
            if isinstance(node_id, str) and node_id:
                task_starts[task_id] = event
        elif event_type in {
            "task_attempt_succeeded",
            "task_attempt_failed",
            "task_attempt_stopped",
            "task_attempt_abandoned",
        } and isinstance(task_id, str):
            task_settlements[task_id] = event
        elif event_type == "task_recovery_routed" and isinstance(task_id, str):
            recovery_routes[task_id] = event

        if event_type == "gate_verdict":
            verdict = str(event.get("verdict", ""))
            if verdict not in _PUSHBACK_VERDICTS or eid is None:
                continue
            reason = event.get("reason")
            gate_verdicts.append(
                GateVerdictRecord(
                    change_id=change_id,
                    seq=int(event["seq"]),  # type: ignore[arg-type]
                    ts=str(event.get("ts", "")),
                    gate=str(event.get("gate", "unknown")),
                    verdict=verdict,
                    reason=reason if isinstance(reason, str) else None,
                    cause=_gate_cause(event),
                    evidence_id=eid,
                )
            )
            evidence_ids.append(eid)
        elif event_type == "task_attempt_succeeded":
            gate_report = event.get("gate_report")
            if not isinstance(gate_report, Mapping) or eid is None:
                continue
            gate_id = gate_report.get("gate_id")
            verdict = gate_report.get("verdict")
            if (
                not isinstance(gate_id, str)
                or not gate_id
                or not isinstance(verdict, str)
                or verdict not in _PUSHBACK_VERDICTS
            ):
                continue
            reason = gate_report.get("reason")
            gate_verdicts.append(
                GateVerdictRecord(
                    change_id=change_id,
                    seq=int(event["seq"]),  # type: ignore[arg-type]
                    ts=str(event.get("ts", "")),
                    gate=gate_id,
                    verdict=verdict,
                    reason=reason if isinstance(reason, str) else None,
                    cause=_gate_cause(gate_report),
                    evidence_id=eid,
                )
            )
            evidence_ids.append(eid)
    events_by_seq = {
        seq: event
        for event in events
        if isinstance((seq := event.get("seq")), int) and not isinstance(seq, bool)
    }
    projection = project_healing_episode_from_events(events)
    for allocation in projection.allocations:
        source_event = events_by_seq.get(allocation.source_seq, {})
        eid = f"{change_id}#seq{allocation.source_seq}"
        healing_allocations.append(
            HealingAllocationRecord(
                change_id=change_id,
                seq=allocation.source_seq,
                ts=str(source_event.get("ts", "")),
                operation_id=allocation.operation_id,
                evidence_id=eid,
            )
        )
        evidence_ids.append(eid)

    healing_applies: list[HealingApplyRecord] = []
    ledger_apply_targets: set[str] = set()
    for record in projection.records:
        allocation = max(
            (item for item in projection.allocations if item.source_seq < record.source_seq),
            key=lambda item: item.source_seq,
            default=None,
        )
        source_event = events_by_seq.get(record.source_seq, {})
        eid = f"{change_id}#seq{record.source_seq}"
        healing_applies.append(
            HealingApplyRecord(
                change_id=change_id,
                target=record.target,
                applied=record.outcome == "applied",
                evidence_id=eid,
                seq=record.source_seq,
                ts=str(source_event.get("ts", "")) or None,
                operation_id=allocation.operation_id if allocation is not None else None,
            )
        )
        ledger_apply_targets.add(record.target)
        evidence_ids.append(eid)

    recovered_task_ids: set[str] = set()
    for failed_task_id, route in recovery_routes.items():
        route_seq = _event_seq(route) or 0
        route_invocation = route.get("invocation_id")
        recovery_node = route.get("via")
        if not isinstance(route_invocation, str) or not isinstance(recovery_node, str):
            continue
        candidates = sorted(
            (
                (_event_seq(started) or 0, recovery_task_id)
                for recovery_task_id, started in task_starts.items()
                if started.get("invocation_id") == route_invocation
                and started.get("node_id") == recovery_node
                and (_event_seq(started) or 0) > route_seq
            ),
            key=lambda item: (item[0], item[1]),
        )
        if not candidates:
            continue
        recovery_task_id = candidates[0][1]
        settlement = task_settlements.get(recovery_task_id)
        if settlement is not None and settlement.get("type") == "task_attempt_succeeded":
            recovered_task_ids.add(failed_task_id)

    task_failures: list[WorkflowTaskFailureRecord] = []
    for task_id, event in sorted(
        task_settlements.items(),
        key=lambda item: _event_seq(item[1]) or 0,
    ):
        if event.get("type") != "task_attempt_failed":
            continue
        started = task_starts.get(task_id)
        node_id = started.get("node_id") if started is not None else None
        eid = _event_evidence_id(change_id, event)
        if not isinstance(node_id, str) or eid is None:
            continue
        raw_attempt_id = event.get("attempt_id")
        attempt_id = raw_attempt_id if isinstance(raw_attempt_id, str) else None
        raw_ts = event.get("ts")
        failure_ts = raw_ts if isinstance(raw_ts, str) else None
        task_failures.append(
            WorkflowTaskFailureRecord(
                change_id=change_id,
                task_id=task_id,
                attempt_id=attempt_id,
                node_id=node_id,
                ts=failure_ts,
                error_kind=event["error_kind"],  # type: ignore[arg-type]
                message=str(event.get("message", "task failed")),
                recovered=task_id in recovered_task_ids,
                evidence_id=eid,
            )
        )
        evidence_ids.append(eid)

    healing_dir = change_dir / "healing"
    if healing_dir.is_dir():
        for path in sorted(healing_dir.glob("*-apply-summary.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            applied = bool(payload.get("applied"))
            target = str(payload.get("target", path.name.split("-", 1)[0]))
            if target in ledger_apply_targets:
                continue
            eid = f"{change_id}#healing:{path.name}"
            healing_applies.append(
                HealingApplyRecord(
                    change_id=change_id,
                    target=target,
                    applied=applied,
                    evidence_id=eid,
                )
            )
            evidence_ids.append(eid)

    skill_loaded_false: list[SkillLoadedFalseRecord] = []
    state_path = existing_with_alias(change_dir / "workflow-state.json")
    if state_path is not None:
        try:
            state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            state = None
        if isinstance(state, dict):
            phases = state.get("phases")
            if isinstance(phases, dict):
                for phase, phase_state in sorted(phases.items()):
                    if not isinstance(phase_state, dict):
                        continue
                    if phase_state.get("skill_loaded") is not False:
                        continue
                    eid = f"{change_id}#workflow-state:{phase}"
                    skill_path = phase_state.get("skill_md_path")
                    expected_skill = None
                    if isinstance(skill_path, str) and skill_path.strip():
                        path = Path(skill_path)
                        expected_skill = path.parent.name if path.name == "SKILL.md" else path.stem
                    skill_loaded_false.append(
                        SkillLoadedFalseRecord(
                            change_id=change_id,
                            phase=str(phase),
                            expected_skill=expected_skill,
                            evidence_id=eid,
                        )
                    )
                    evidence_ids.append(eid)

    head_seq = None
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            head_seq = seq
    head_event_id = f"{change_id}#seq{head_seq}" if head_seq is not None else None
    source = RetroSourceDescriptor(
        kind="workflow_ledger",
        change_id=change_id,
        head_event_id=head_event_id,
        sha256=ledger_sha,
        evidence_ids=tuple(sorted(set(evidence_ids))),
    )
    return (
        source,
        gate_verdicts,
        healing_allocations,
        healing_applies,
        skill_loaded_false,
        task_failures,
        None,
    )


class LedgerWorkflowHistoryReader:
    """Production adapter: archive-first Change ledgers, event-ts terminal ordering."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def discover_change_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        archive = archive_root(self._root)
        active = changes_root(self._root)
        if archive.is_dir():
            ids.update(_list_dir_names(archive))
        if active.is_dir():
            ids.update(_list_dir_names(active))
        return frozenset(ids)

    def list_terminal_changes(
        self,
        change_ids: tuple[str, ...] | None = None,
        *,
        tolerate_member_errors: bool = False,
    ) -> tuple[TerminalChangeRef, ...]:
        refs: list[TerminalChangeRef] = []
        selected = self.discover_change_ids() if change_ids is None else frozenset(change_ids)
        for change_id in sorted(selected):
            try:
                loc = resolve_change(self._root, change_id, prefer="archive")
            except ChangeNotFoundError:
                continue
            try:
                data, events = _read_ledger_strict(change_id, loc.path)
            except WorkflowHistoryIntegrityError:
                if not tolerate_member_errors:
                    raise
                continue
            archived = loc.source == "archive"
            ref = _terminal_from_events(
                change_id,
                events,
                _sha256_bytes(data) if data else "",
                archived=archived,
            )
            if ref is not None:
                refs.append(ref)
        refs.sort(key=lambda item: (item.terminal_ts, item.change_id))
        return tuple(refs)

    def read_window(self, window: ResolvedRetroWindow) -> WorkflowEvidenceSlice:
        sources: list[RetroSourceDescriptor] = []
        gate_verdicts: list[GateVerdictRecord] = []
        healing_allocations: list[HealingAllocationRecord] = []
        healing_applies: list[HealingApplyRecord] = []
        skill_loaded_false: list[SkillLoadedFalseRecord] = []
        task_failures: list[WorkflowTaskFailureRecord] = []
        reasons: list[str] = []
        statuses = (
            {member.change_id: member.execution_status for member in window.batch_scope.members}
            if window.batch_scope is not None
            else {}
        )

        for change_id in window.change_ids:
            status = statuses.get(change_id, "failed")
            if window.batch_scope is not None and status in {"running", "not_started"}:
                reason_code = "non_terminal" if status == "running" else "workspace_missing"
                reasons.append(_batch_gap_reason(change_id, status, reason_code))
                continue
            try:
                loc = resolve_change(self._root, change_id, prefer="archive")
            except ChangeNotFoundError:
                reasons.append(
                    _batch_gap_reason(change_id, status, "workspace_missing")
                    if window.batch_scope is not None
                    else f"workflow_source_missing:{change_id}"
                )
                continue
            try:
                (
                    source,
                    gates,
                    allocations,
                    applies,
                    skills,
                    failures,
                    error,
                ) = _extract_from_change(change_id, loc.path)
            except WorkflowHistoryIntegrityError:
                if window.batch_scope is None:
                    raise
                reasons.append(_batch_gap_reason(change_id, status, "ledger_corrupt"))
                continue
            if error is not None:
                reasons.append(
                    _batch_gap_reason(change_id, status, "ledger_missing")
                    if window.batch_scope is not None
                    else error
                )
                continue
            if source is not None:
                sources.append(source)
            gate_verdicts.extend(gates)
            healing_allocations.extend(allocations)
            healing_applies.extend(applies)
            skill_loaded_false.extend(skills)
            task_failures.extend(failures)
            if any(
                failure.node_id == "inspect-with-issues" and not failure.recovered for failure in failures
            ):
                reasons.append(f"issue_pipeline_failed:{change_id}")

        integrity = (
            RetroIntegrity(status="incomplete", reasons=tuple(reasons))
            if reasons
            else RetroIntegrity(status="complete")
        )
        return WorkflowEvidenceSlice(
            change_ids=window.change_ids,
            sources=tuple(sources),
            integrity=integrity,
            gate_verdicts=tuple(gate_verdicts),
            healing_allocations=tuple(healing_allocations),
            healing_applies=tuple(healing_applies),
            skill_loaded_false=tuple(skill_loaded_false),
            task_failures=tuple(task_failures),
        )


class InMemoryWorkflowHistoryReader:
    """Test adapter backed by terminal refs and/or a frozen evidence slice."""

    def __init__(
        self,
        *,
        terminals: Sequence[TerminalChangeRef] = (),
        slice_: WorkflowEvidenceSlice | None = None,
        known_ids: frozenset[str] | None = None,
    ) -> None:
        self._terminals = tuple(sorted(terminals, key=lambda item: (item.terminal_ts, item.change_id)))
        self._slice = slice_
        discovered = set(known_ids or ())
        discovered.update(item.change_id for item in self._terminals)
        if slice_ is not None:
            discovered.update(slice_.change_ids)
            for source in slice_.sources:
                if source.change_id:
                    discovered.add(source.change_id)
        self._known = frozenset(discovered)

    @classmethod
    def from_terminals(cls, terminals: Sequence[TerminalChangeRef]) -> InMemoryWorkflowHistoryReader:
        return cls(terminals=terminals)

    @classmethod
    def from_slice(cls, slice_: WorkflowEvidenceSlice) -> InMemoryWorkflowHistoryReader:
        terminals = tuple(
            TerminalChangeRef(
                change_id=change_id,
                terminal_ts="1970-01-01T00:00:00Z",
                ledger_sha256=next(
                    (s.sha256 for s in slice_.sources if s.change_id == change_id),
                    "",
                ),
            )
            for change_id in slice_.change_ids
        )
        return cls(terminals=terminals, slice_=slice_, known_ids=frozenset(slice_.change_ids))

    def discover_change_ids(self) -> frozenset[str]:
        return self._known

    def list_terminal_changes(
        self,
        change_ids: tuple[str, ...] | None = None,
        *,
        tolerate_member_errors: bool = False,
    ) -> tuple[TerminalChangeRef, ...]:
        del tolerate_member_errors
        if change_ids is None:
            return self._terminals
        selected = frozenset(change_ids)
        return tuple(item for item in self._terminals if item.change_id in selected)

    def read_window(self, window: ResolvedRetroWindow) -> WorkflowEvidenceSlice:
        if self._slice is not None and self._slice.change_ids == window.change_ids:
            return self._slice
        reasons = [
            f"workflow_source_missing:{change_id}"
            for change_id in window.change_ids
            if change_id not in self._known
        ]
        integrity = (
            RetroIntegrity(status="incomplete", reasons=tuple(reasons))
            if reasons
            else RetroIntegrity(status="complete")
        )
        return WorkflowEvidenceSlice(change_ids=window.change_ids, integrity=integrity)
