"""Pure quarantine projection fold (M3 Task 4 / §5-C3 isolation).

Enter when seed-replay rate < 1.0 (flaky) with ≥1 receipt path; release only
after ``release_requires`` consecutive successful replays (outcome ``violate``;
default N=2). Deterministic entry ordering is enforced by the projection model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

from assurance_kernel.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_kernel.artifacts.models.quarantine import (
    DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    QuarantineEntry,
    QuarantineProjection,
    QuarantineSubjectKind,
)
from assurance_kernel.evidence.replay_telemetry import (
    compute_seed_replay_rate,
    replay_receipt_relpath,
)

QuarantineEventKind = Literal["enter", "success", "release"]


class QuarantineEvent(TypedDict, total=False):
    kind: QuarantineEventKind
    subject_kind: QuarantineSubjectKind
    subject_key: str
    reason: str
    entered_at: str
    evidence_refs: tuple[str, ...]
    release_requires: int


def propose_enter_quarantine(
    *,
    subject_kind: QuarantineSubjectKind,
    subject_key: str,
    reason: str,
    entered_at: str,
    evidence_refs: Sequence[str],
    release_requires: int = DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
) -> QuarantineEntry | None:
    """Propose an ``active`` entry; ``None`` when evidence_refs is empty (fail-closed)."""
    refs = tuple(str(item) for item in evidence_refs if str(item))
    if not refs:
        return None
    return QuarantineEntry(
        subject_kind=subject_kind,
        subject_key=subject_key,
        status="active",
        reason=reason,
        entered_at=entered_at,
        evidence_refs=refs,
        release_requires=release_requires,
        consecutive_successes=0,
    )


def propose_enter_from_receipts(
    *,
    subject_kind: QuarantineSubjectKind,
    subject_key: str,
    receipts: Sequence[ReplayAttemptReceipt],
    evidence_refs: Sequence[str],
    entered_at: str,
    release_requires: int = DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
) -> QuarantineEntry | None:
    """Enter when C3 rate is defined and < 1.0 (flaky)."""
    _success, _attempts, rate = compute_seed_replay_rate(receipts)
    if rate is None or rate >= 1.0:
        return None
    return propose_enter_quarantine(
        subject_kind=subject_kind,
        subject_key=subject_key,
        reason=f"seed_replay_rate={rate}",
        entered_at=entered_at,
        evidence_refs=evidence_refs,
        release_requires=release_requires,
    )


def consecutive_replay_successes(receipts: Sequence[ReplayAttemptReceipt]) -> int:
    """Trailing streak within one CE; multiple CE timelines have no global order."""
    if len({receipt.counterexample_id for receipt in receipts}) > 1:
        # attempt_index restarts for each CE. Treat an unorderable aggregate as
        # no release evidence rather than fabricating a cross-CE chronology.
        return 0
    ordered = sorted(receipts, key=lambda item: item.attempt_index)
    count = 0
    for receipt in reversed(ordered):
        if receipt.outcome == "violate":
            count += 1
        else:
            break
    return count


def active_subject_keys(projection: QuarantineProjection | None) -> frozenset[str]:
    if projection is None:
        return frozenset()
    return frozenset(entry.subject_key for entry in projection.entries if entry.status == "active")


def apply_quarantine_event(
    projection: QuarantineProjection,
    event: Mapping[str, object],
) -> QuarantineProjection:
    """Pure fold of one enter/success/release event over a projection."""
    kind = str(event.get("kind") or "")
    subject_kind = str(event.get("subject_kind") or "")
    subject_key = str(event.get("subject_key") or "")
    if kind not in {"enter", "success", "release"}:
        raise ValueError(f"unknown quarantine event kind: {kind!r}")
    if subject_kind not in {"property", "journey", "obligation_key"} or not subject_key:
        raise ValueError("quarantine event requires subject_kind and subject_key")

    by_key = {(entry.subject_kind, entry.subject_key): entry for entry in projection.entries}

    if kind == "enter":
        refs = event.get("evidence_refs") or ()
        if not isinstance(refs, (list, tuple)):
            raise ValueError("enter event evidence_refs must be a sequence")
        release_raw = event.get("release_requires")
        if isinstance(release_raw, int):
            release_requires = release_raw
        elif isinstance(release_raw, str) and release_raw.isdigit():
            release_requires = int(release_raw)
        else:
            release_requires = DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES
        proposed = propose_enter_quarantine(
            subject_kind=subject_kind,  # type: ignore[arg-type]
            subject_key=subject_key,
            reason=str(event.get("reason") or "explicit_enter"),
            entered_at=str(event.get("entered_at") or ""),
            evidence_refs=tuple(str(item) for item in refs),
            release_requires=release_requires,
        )
        if proposed is None:
            raise ValueError("enter requires >=1 replay receipt evidence_ref")
        if not proposed.entered_at:
            raise ValueError("enter requires entered_at")
        by_key[(proposed.subject_kind, proposed.subject_key)] = proposed
    else:
        key = (subject_kind, subject_key)  # type: ignore[assignment]
        current = by_key.get(key)  # type: ignore[arg-type]
        if current is None or current.status != "active":
            raise ValueError(f"no active quarantine for {subject_kind}:{subject_key}")
        if kind == "success":
            by_key[key] = current.model_copy(  # type: ignore[index]
                update={"consecutive_successes": current.consecutive_successes + 1}
            )
        else:  # release
            if current.consecutive_successes < current.release_requires:
                raise ValueError(
                    "cannot release: consecutive_successes "
                    f"{current.consecutive_successes} < release_requires "
                    f"{current.release_requires}"
                )
            by_key[key] = current.model_copy(update={"status": "released"})  # type: ignore[index]

    return QuarantineProjection(
        schema_version=projection.schema_version,
        change_id=projection.change_id,
        entries=tuple(by_key.values()),
    )


def infer_subject_kind(obligation_id: str) -> QuarantineSubjectKind:
    """Map a CE obligation id to a quarantine subject kind."""
    if obligation_id.startswith("entities.") and ".constraints." in obligation_id:
        return "property"
    if obligation_id.startswith("entities.") or obligation_id.startswith("auth."):
        return "obligation_key"
    return "journey"


def fold_receipts_into_projection(
    prior: QuarantineProjection | None,
    *,
    change_id: str,
    subject_receipts: Mapping[tuple[QuarantineSubjectKind, str], Sequence[ReplayAttemptReceipt]],
    entered_at: str,
    release_requires: int = DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
) -> QuarantineProjection:
    """Fold per-subject receipt aggregates into a new projection.

    - Flaky rate (< 1.0) → enter (or refresh active evidence_refs).
    - Active subjects update ``consecutive_successes`` from trailing violate streak.
    - Auto-release when streak meets ``release_requires``.
    """
    base = prior or QuarantineProjection(schema_version="1", change_id=change_id, entries=())
    by_key = {(entry.subject_kind, entry.subject_key): entry for entry in base.entries}

    for (subject_kind, subject_key), receipts in sorted(subject_receipts.items()):
        refs = tuple(
            replay_receipt_relpath(
                counterexample_id=receipt.counterexample_id,
                attempt_index=receipt.attempt_index,
            )
            for receipt in sorted(receipts, key=lambda item: item.attempt_index)
        )
        streak = consecutive_replay_successes(receipts)
        current = by_key.get((subject_kind, subject_key))

        if current is not None and current.status == "active":
            updated = current.model_copy(
                update={
                    "consecutive_successes": streak,
                    "evidence_refs": refs or current.evidence_refs,
                }
            )
            if updated.consecutive_successes >= updated.release_requires:
                updated = updated.model_copy(update={"status": "released"})
            by_key[(subject_kind, subject_key)] = updated
            continue

        if current is not None and current.status == "released":
            known_refs = frozenset(current.evidence_refs)
            new_receipts = tuple(
                receipt
                for receipt in receipts
                if replay_receipt_relpath(
                    counterexample_id=receipt.counterexample_id,
                    attempt_index=receipt.attempt_index,
                )
                not in known_refs
            )
            if not new_receipts:
                # Folding the same immutable receipt set must be idempotent.
                continue
            if any(receipt.outcome != "violate" for receipt in new_receipts):
                proposed = propose_enter_from_receipts(
                    subject_kind=subject_kind,
                    subject_key=subject_key,
                    receipts=new_receipts,
                    evidence_refs=refs,
                    entered_at=entered_at,
                    release_requires=release_requires,
                )
                if proposed is not None:
                    by_key[(subject_kind, subject_key)] = proposed
                continue
            # New successful evidence advances the audit trail but cannot make
            # an already released subject active again.
            by_key[(subject_kind, subject_key)] = current.model_copy(
                update={
                    "evidence_refs": refs,
                    "consecutive_successes": max(current.consecutive_successes, streak),
                }
            )
            continue

        proposed = propose_enter_from_receipts(
            subject_kind=subject_kind,
            subject_key=subject_key,
            receipts=receipts,
            evidence_refs=refs,
            entered_at=entered_at,
            release_requires=release_requires,
        )
        if proposed is not None:
            by_key[(subject_kind, subject_key)] = proposed

    return QuarantineProjection(
        schema_version="1",
        change_id=change_id,
        entries=tuple(by_key.values()),
    )


__all__ = [
    "QuarantineEvent",
    "active_subject_keys",
    "apply_quarantine_event",
    "consecutive_replay_successes",
    "fold_receipts_into_projection",
    "infer_subject_kind",
    "propose_enter_from_receipts",
    "propose_enter_quarantine",
]
