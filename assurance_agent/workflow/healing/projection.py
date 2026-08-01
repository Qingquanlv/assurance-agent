"""Normalized healing episode projection over legacy and v2 ledger events.

Direct raw legacy event-name matching is permitted only in ``core/events.py``,
this module, and versioned fixture tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import Ledger, event_seq

LegacyEventName = Literal[
    "healing_entry_baseline_pinned",
    "healing_attempt_allocated",
    "heal_record_apply",
]

V2EventName = Literal[
    "healing_attempt_allocated_v2",
    "fixer_proposal_approved",
    "heal_record_apply_v2",
]

LEGACY_BASELINE = "healing_entry_baseline_pinned"
LEGACY_ALLOCATION = "healing_attempt_allocated"
LEGACY_RECORD = "heal_record_apply"
V2_ALLOCATION = "healing_attempt_allocated_v2"
V2_APPROVAL = "fixer_proposal_approved"
V2_RECORD = "heal_record_apply_v2"


class HealingProjectionIntegrityError(AaError):
    """Ledger conflict while projecting a healing episode."""


@dataclass(frozen=True, slots=True)
class ProjectedBaseline:
    episode_id: str
    entry_batch_id: str
    artifact_sha256: str
    artifact_file: str
    source_seq: int
    form: Literal["legacy", "v2"]


@dataclass(frozen=True, slots=True)
class ProjectedAllocation:
    logical_key: str
    episode_id: str
    attempt_id: str
    attempt_number: int
    operation_id: str
    source_batch_id: str
    baseline_sha256: str
    entry_batch_id: str
    source_seq: int
    form: Literal["legacy", "v2"]


@dataclass(frozen=True, slots=True)
class ProjectedApproval:
    approval_id: str
    root_invocation_id: str
    interrupt_task_id: str
    source_gate_attempt_id: str
    source_tree_id: str
    proposal_sha256: str
    fixer_authority_sha256: str
    entry_baseline_sha256: str
    policy_sha256: str
    targets: tuple[Literal["api", "e2e"], ...]
    paths: tuple[str, ...]
    target_tree_id: str
    source_seq: int


@dataclass(frozen=True, slots=True)
class ProjectedRecord:
    record_key: str
    target: Literal["api", "e2e"]
    outcome: str
    proposal_ids: tuple[str, ...]
    claimed_modified_paths: tuple[str, ...]
    intent_sha256: str | None
    write_set_id: str | None
    safety_payload_sha256: str | None
    files_modified: tuple[str, ...]
    source_seq: int
    form: Literal["legacy", "v2"]
    root_invocation_id: str | None = None
    record_task_id: str | None = None
    fixer_attempt_id: str | None = None
    entry_batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class HealingEpisodeProjection:
    baseline: ProjectedBaseline | None
    allocations: tuple[ProjectedAllocation, ...]
    approvals: tuple[ProjectedApproval, ...]
    records: tuple[ProjectedRecord, ...]

    @property
    def attempts_used(self) -> int:
        return len(self.allocations)

    @property
    def episode_id(self) -> str | None:
        return self.baseline.episode_id if self.baseline is not None else None

    @property
    def latest_allocation(self) -> ProjectedAllocation | None:
        if not self.allocations:
            return None
        return max(self.allocations, key=lambda item: item.source_seq)


def project_healing_episode(
    change_dir: Path,
    *,
    episode_id: str | None = None,
) -> HealingEpisodeProjection:
    """Project the active (or specified) healing episode from the host ledger."""
    events = Ledger(change_dir).all()
    return project_healing_episode_from_events(events, episode_id=episode_id)


def project_healing_episode_from_events(
    events: Sequence[Mapping[str, object]],
    *,
    episode_id: str | None = None,
) -> HealingEpisodeProjection:
    ordered = sorted(events, key=event_seq)
    baseline = _select_baseline(ordered, episode_id=episode_id)
    active_episode = baseline.episode_id if baseline is not None else episode_id
    if active_episode is None:
        active_episode = _infer_latest_episode_id(ordered)
    allocations = _project_allocations(ordered, episode_id=active_episode, baseline=baseline)
    approvals = _project_approvals(ordered)
    records = _project_records(ordered, after_seq=_allocation_floor(allocations, baseline))
    return HealingEpisodeProjection(
        baseline=baseline,
        allocations=allocations,
        approvals=approvals,
        records=records,
    )


def _infer_latest_episode_id(events: Sequence[Mapping[str, object]]) -> str | None:
    latest: tuple[int, str] | None = None
    for event in events:
        if event.get("type") not in {LEGACY_ALLOCATION, V2_ALLOCATION, LEGACY_BASELINE}:
            continue
        episode = event.get("episode_id")
        if not isinstance(episode, str) or not episode:
            continue
        seq = event_seq(event)
        if latest is None or seq >= latest[0]:
            latest = (seq, episode)
    return latest[1] if latest is not None else None


def _select_baseline(
    events: Sequence[Mapping[str, object]],
    *,
    episode_id: str | None,
) -> ProjectedBaseline | None:
    legacy: list[ProjectedBaseline] = []
    for event in events:
        if event.get("type") != LEGACY_BASELINE:
            continue
        ep = str(event["episode_id"])
        if episode_id is not None and ep != episode_id:
            continue
        legacy.append(
            ProjectedBaseline(
                episode_id=ep,
                entry_batch_id=str(event["entry_batch_id"]),
                artifact_sha256=str(event["artifact_sha256"]),
                artifact_file=str(event.get("artifact_file", "healing/entry-baseline.json")),
                source_seq=event_seq(event),
                form="legacy",
            )
        )
    v2_baselines: list[ProjectedBaseline] = []
    for event in events:
        if event.get("type") != V2_ALLOCATION:
            continue
        if not bool(event.get("baseline_embedded")):
            continue
        ep = str(event["episode_id"])
        if episode_id is not None and ep != episode_id:
            continue
        v2_baselines.append(
            ProjectedBaseline(
                episode_id=ep,
                entry_batch_id=str(event["entry_batch_id"]),
                artifact_sha256=str(event["baseline_sha256"]),
                artifact_file="healing/entry-baseline.json",
                source_seq=event_seq(event),
                form="v2",
            )
        )
    candidates = [*legacy, *v2_baselines]
    if not candidates:
        return None
    if episode_id is None:
        # Latest episode by baseline source sequence.
        return max(candidates, key=lambda item: item.source_seq)
    matching = [item for item in candidates if item.episode_id == episode_id]
    if not matching:
        return None
    # One episode may have legacy+v2 baseline claims only when equivalent.
    chosen = matching[0]
    for other in matching[1:]:
        if (
            other.entry_batch_id != chosen.entry_batch_id
            or other.artifact_sha256 != chosen.artifact_sha256
        ):
            raise HealingProjectionIntegrityError(
                f"conflicting baselines for episode {episode_id}"
            )
    return min(matching, key=lambda item: item.source_seq)


def _project_allocations(
    events: Sequence[Mapping[str, object]],
    *,
    episode_id: str | None,
    baseline: ProjectedBaseline | None,
) -> tuple[ProjectedAllocation, ...]:
    if episode_id is None:
        return ()
    collected: list[ProjectedAllocation] = []
    for event in events:
        event_type = event.get("type")
        if event_type == LEGACY_ALLOCATION:
            if str(event.get("episode_id")) != episode_id:
                continue
            baseline_sha = baseline.artifact_sha256 if baseline is not None else ""
            entry_batch = baseline.entry_batch_id if baseline is not None else ""
            collected.append(
                ProjectedAllocation(
                    logical_key=str(event["operation_id"]),
                    episode_id=episode_id,
                    attempt_id=str(event["attempt_id"]),
                    attempt_number=int(event["attempt_number"]),  # type: ignore[arg-type]
                    operation_id=str(event["operation_id"]),
                    source_batch_id=str(event["source_batch_id"]),
                    baseline_sha256=baseline_sha,
                    entry_batch_id=entry_batch,
                    source_seq=event_seq(event),
                    form="legacy",
                )
            )
        elif event_type == V2_ALLOCATION:
            if str(event.get("episode_id")) != episode_id:
                continue
            collected.append(
                ProjectedAllocation(
                    logical_key=str(event["operation_id"]),
                    episode_id=episode_id,
                    attempt_id=str(event["attempt_id"]),
                    attempt_number=int(event["attempt_number"]),  # type: ignore[arg-type]
                    operation_id=str(event["operation_id"]),
                    source_batch_id=str(event["source_batch_id"]),
                    baseline_sha256=str(event["baseline_sha256"]),
                    entry_batch_id=str(event["entry_batch_id"]),
                    source_seq=event_seq(event),
                    form="v2",
                )
            )
    return _dedupe_allocations(collected, baseline=baseline)


def _dedupe_allocations(
    allocations: Sequence[ProjectedAllocation],
    *,
    baseline: ProjectedBaseline | None,
) -> tuple[ProjectedAllocation, ...]:
    by_key: dict[str, ProjectedAllocation] = {}
    for item in sorted(allocations, key=lambda value: value.source_seq):
        existing = by_key.get(item.logical_key)
        if existing is None:
            if (
                baseline is not None
                and item.baseline_sha256
                and item.baseline_sha256 != baseline.artifact_sha256
            ):
                raise HealingProjectionIntegrityError(
                    f"allocation baseline conflict for key {item.logical_key}"
                )
            by_key[item.logical_key] = item
            continue
        if _allocation_payload_equal(existing, item):
            # Prefer the earlier source sequence; keep first form.
            continue
        raise HealingProjectionIntegrityError(
            f"allocation payload/ID conflict for logical key {item.logical_key}"
        )
    return tuple(sorted(by_key.values(), key=lambda value: value.source_seq))


def _allocation_payload_equal(left: ProjectedAllocation, right: ProjectedAllocation) -> bool:
    return (
        left.episode_id == right.episode_id
        and left.attempt_id == right.attempt_id
        and left.attempt_number == right.attempt_number
        and left.operation_id == right.operation_id
        and left.source_batch_id == right.source_batch_id
        and left.baseline_sha256 == right.baseline_sha256
        and left.entry_batch_id == right.entry_batch_id
    )


def _project_approvals(events: Sequence[Mapping[str, object]]) -> tuple[ProjectedApproval, ...]:
    out: list[ProjectedApproval] = []
    for event in events:
        if event.get("type") != V2_APPROVAL:
            continue
        targets_raw = event.get("targets") or []
        paths_raw = event.get("paths") or []
        if not isinstance(targets_raw, list) or not isinstance(paths_raw, list):
            raise HealingProjectionIntegrityError("malformed fixer_proposal_approved targets/paths")
        targets = tuple(str(item) for item in targets_raw)  # type: ignore[misc]
        out.append(
            ProjectedApproval(
                approval_id=str(event["approval_id"]),
                root_invocation_id=str(event["root_invocation_id"]),
                interrupt_task_id=str(event["interrupt_task_id"]),
                source_gate_attempt_id=str(event["source_gate_attempt_id"]),
                source_tree_id=str(event["source_tree_id"]),
                proposal_sha256=str(event["proposal_sha256"]),
                fixer_authority_sha256=str(event["fixer_authority_sha256"]),
                entry_baseline_sha256=str(event["entry_baseline_sha256"]),
                policy_sha256=str(event["policy_sha256"]),
                targets=targets,  # type: ignore[arg-type]
                paths=tuple(str(item) for item in paths_raw),
                target_tree_id=str(event["target_tree_id"]),
                source_seq=event_seq(event),
            )
        )
    return tuple(sorted(out, key=lambda item: item.source_seq))


def _project_records(
    events: Sequence[Mapping[str, object]],
    *,
    after_seq: int,
) -> tuple[ProjectedRecord, ...]:
    by_key: dict[str, ProjectedRecord] = {}
    for event in events:
        if event_seq(event) <= after_seq:
            continue
        event_type = event.get("type")
        if event_type == LEGACY_RECORD:
            key = str(event.get("attempt_key") or f"legacy:{event_seq(event)}")
            record = ProjectedRecord(
                record_key=key,
                target=str(event["target"]),  # type: ignore[arg-type]
                outcome="applied" if event.get("files_modified") else "no_op",
                proposal_ids=(),
                claimed_modified_paths=tuple(str(p) for p in (event.get("files_modified") or [])),  # type: ignore[arg-type]
                intent_sha256=None,
                write_set_id=None,
                safety_payload_sha256=None,
                files_modified=tuple(str(p) for p in (event.get("files_modified") or [])),  # type: ignore[arg-type]
                source_seq=event_seq(event),
                form="legacy",
            )
        elif event_type == V2_RECORD:
            key = str(event["record_key"])
            record = ProjectedRecord(
                record_key=key,
                target=str(event["target"]),  # type: ignore[arg-type]
                outcome=str(event["outcome"]),
                proposal_ids=tuple(str(p) for p in (event.get("proposal_ids") or [])),  # type: ignore[arg-type]
                claimed_modified_paths=tuple(
                    str(p) for p in (event.get("claimed_modified_paths") or [])  # type: ignore[arg-type]
                ),
                intent_sha256=str(event["intent_sha256"]),
                write_set_id=str(event["write_set_id"]),
                safety_payload_sha256=str(event["safety_payload_sha256"]),
                files_modified=tuple(
                    str(p) for p in (event.get("claimed_modified_paths") or [])  # type: ignore[arg-type]
                ),
                source_seq=event_seq(event),
                form="v2",
                root_invocation_id=str(event.get("root_invocation_id") or "") or None,
                record_task_id=str(event.get("record_task_id") or "") or None,
                fixer_attempt_id=str(event.get("fixer_attempt_id") or "") or None,
                entry_batch_id=str(event.get("entry_batch_id") or "") or None,
            )
        else:
            continue
        existing = by_key.get(record.record_key)
        if existing is None:
            by_key[record.record_key] = record
            continue
        if existing.form == "v2" and record.form == "v2":
            if (
                existing.outcome != record.outcome
                or existing.intent_sha256 != record.intent_sha256
                or existing.write_set_id != record.write_set_id
                or existing.safety_payload_sha256 != record.safety_payload_sha256
            ):
                raise HealingProjectionIntegrityError(
                    f"record payload conflict for key {record.record_key}"
                )
            continue
        if existing.files_modified != record.files_modified or existing.target != record.target:
            raise HealingProjectionIntegrityError(
                f"record payload conflict for key {record.record_key}"
            )
    return tuple(sorted(by_key.values(), key=lambda item: item.source_seq))


def _allocation_floor(
    allocations: Sequence[ProjectedAllocation],
    baseline: ProjectedBaseline | None,
) -> int:
    if allocations:
        return max(item.source_seq for item in allocations)
    if baseline is not None:
        return baseline.source_seq
    return 0


__all__ = [
    "HealingEpisodeProjection",
    "HealingProjectionIntegrityError",
    "LEGACY_ALLOCATION",
    "LEGACY_BASELINE",
    "LEGACY_RECORD",
    "ProjectedAllocation",
    "ProjectedApproval",
    "ProjectedBaseline",
    "ProjectedRecord",
    "V2_ALLOCATION",
    "V2_APPROVAL",
    "V2_RECORD",
    "project_healing_episode",
    "project_healing_episode_from_events",
]
