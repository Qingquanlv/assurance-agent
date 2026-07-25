"""Read-only Workflow history adapters for Retro window resolution and evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.change_location import (
    ChangeNotFoundError,
    archive_root,
    changes_root,
    resolve_change,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.nightly.utils import list_dir_names
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict

if TYPE_CHECKING:
    from assurance_agent.retro.window import ResolvedRetroWindow

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_TERMINAL_TYPES = frozenset({"graph_completed", "graph_stopped", "graph_failed"})
_PUSHBACK_VERDICTS = frozenset({"needs_fix", "fail", "blocked", "stop"})


class WorkflowHistoryIntegrityError(AaError):
    """Raised when a Change workflow ledger is corrupt or unreadable."""


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


class SkillLoadedFalseRecord(BaseModel):
    model_config = _FROZEN

    change_id: str
    phase: str
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

    def resolvable_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for source in self.sources:
            ids.update(source.evidence_ids)
        ids.update(record.evidence_id for record in self.gate_verdicts)
        ids.update(record.evidence_id for record in self.healing_allocations)
        ids.update(record.evidence_id for record in self.healing_applies)
        ids.update(record.evidence_id for record in self.skill_loaded_false)
        return frozenset(ids)


@runtime_checkable
class WorkflowHistoryReader(Protocol):
    def list_terminal_changes(self) -> tuple[TerminalChangeRef, ...]: ...

    def discover_change_ids(self) -> frozenset[str]: ...

    def read_window(self, window: ResolvedRetroWindow) -> WorkflowEvidenceSlice: ...


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _event_evidence_id(change_id: str, event: Mapping[str, object]) -> str | None:
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return f"{change_id}#seq{seq}"
    return None


def _read_ledger_strict(change_id: str, change_dir: Path) -> tuple[bytes, list[dict[str, object]]]:
    """Strict envelope read; never soft-skips malformed JSONL lines."""
    events_path = change_dir / "events.jsonl"
    if not events_path.is_file():
        return b"", []
    try:
        data = events_path.read_bytes()
    except OSError as exc:
        raise WorkflowHistoryIntegrityError(
            f"workflow_ledger_corrupt:{change_id}: {exc}"
        ) from exc
    if not data:
        return data, []
    try:
        events = read_events_strict(change_dir)
    except (LedgerIntegrityError, UnicodeDecodeError) as exc:
        raise WorkflowHistoryIntegrityError(
            f"workflow_ledger_corrupt:{change_id}: {exc}"
        ) from exc
    return data, events


def _terminal_from_events(
    change_id: str,
    events: Sequence[Mapping[str, object]],
    ledger_sha256: str,
    *,
    archived: bool,
) -> TerminalChangeRef | None:
    terminal_event: Mapping[str, object] | None = None
    for event in events:
        if event.get("type") in _TERMINAL_TYPES:
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
    str | None,
]:
    events_path = change_dir / "events.jsonl"
    if not events_path.is_file():
        return None, [], [], [], [], f"workflow_source_missing:{change_id}"
    data, events = _read_ledger_strict(change_id, change_dir)

    ledger_sha = _sha256_bytes(data)
    gate_verdicts: list[GateVerdictRecord] = []
    healing_allocations: list[HealingAllocationRecord] = []
    evidence_ids: list[str] = []
    for event in events:
        eid = _event_evidence_id(change_id, event)
        event_type = event.get("type")
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
                    evidence_id=eid,
                )
            )
            evidence_ids.append(eid)
        elif event_type == "healing_attempt_allocated":
            operation_id = event.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id or eid is None:
                continue
            healing_allocations.append(
                HealingAllocationRecord(
                    change_id=change_id,
                    seq=int(event["seq"]),  # type: ignore[arg-type]
                    ts=str(event.get("ts", "")),
                    operation_id=operation_id,
                    evidence_id=eid,
                )
            )
            evidence_ids.append(eid)

    healing_applies: list[HealingApplyRecord] = []
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
    state_path = change_dir / "workflow-state.yaml"
    if state_path.is_file():
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
                    skill_loaded_false.append(
                        SkillLoadedFalseRecord(
                            change_id=change_id,
                            phase=str(phase),
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
    return source, gate_verdicts, healing_allocations, healing_applies, skill_loaded_false, None


class LedgerWorkflowHistoryReader:
    """Production adapter: archive-first Change ledgers, event-ts terminal ordering."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def discover_change_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        archive = archive_root(self._root)
        active = changes_root(self._root)
        if archive.is_dir():
            ids.update(list_dir_names(archive))
        if active.is_dir():
            ids.update(list_dir_names(active))
        return frozenset(ids)

    def list_terminal_changes(self) -> tuple[TerminalChangeRef, ...]:
        refs: list[TerminalChangeRef] = []
        for change_id in sorted(self.discover_change_ids()):
            try:
                loc = resolve_change(self._root, change_id, prefer="archive")
            except ChangeNotFoundError:
                continue
            data, events = _read_ledger_strict(change_id, loc.path)
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
        reasons: list[str] = []

        for change_id in window.change_ids:
            try:
                loc = resolve_change(self._root, change_id, prefer="archive")
            except ChangeNotFoundError:
                reasons.append(f"workflow_source_missing:{change_id}")
                continue
            (
                source,
                gates,
                allocations,
                applies,
                skills,
                error,
            ) = _extract_from_change(change_id, loc.path)
            if error is not None:
                reasons.append(error)
                continue
            if source is not None:
                sources.append(source)
            gate_verdicts.extend(gates)
            healing_allocations.extend(allocations)
            healing_applies.extend(applies)
            skill_loaded_false.extend(skills)

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

    def list_terminal_changes(self) -> tuple[TerminalChangeRef, ...]:
        return self._terminals

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
