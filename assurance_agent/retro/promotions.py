"""Append-only promotion event stream (``promotions.json``, schema_version "2").

Retro review decisions, eval outcomes and memory applications are recorded as
an append-only event stream so the full approval history can be replayed
(spec §6). Writes go through a temp file + ``os.replace`` so a crash never
leaves a truncated ``promotions.json``.

Readers tolerate the two pre-event formats and coerce them into
``review_decision`` events:

- a bare list of legacy ``RetroPromoteRecord`` dicts (``[{...}, ...]``)
- a dict with a ``promotions`` list (``{"promotions": [...]}``)

Writes always upgrade the file to ``{"schema_version": "2", "events": [...]}``.

Event shapes (all carry ``proposal_id``, ``type``, ``actor``, ``at``)::

    review_decision: + decision (promoted|rejected|needs_rework), rework_note?
    eval_completed:  + result (pass|regression|inconclusive|error), run_ids, gate?, note?
    application:     + result (applied|rolled_back), target?, content_sha256?, note?

State machine (spec §6.4):
``proposed -> rejected|needs_rework|promoted_pending_eval``;
``promoted_pending_eval -> applied|rolled_back|awaiting_baseline|eval_error``.
``applied``/``rejected`` are terminal; ``rolled_back`` is treated as
``needs_rework``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "2"

EVENT_REVIEW_DECISION = "review_decision"
EVENT_EVAL_COMPLETED = "eval_completed"
EVENT_APPLICATION = "application"

TERMINAL_STATES = frozenset({"applied", "rejected"})
# `rolled_back` proposals are treated as `needs_rework` (spec §6.4).
NEEDS_REWORK_STATES = frozenset({"needs_rework", "rolled_back"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _legacy_record_to_event(record: dict) -> dict:
    """Coerce a legacy RetroPromoteRecord dict into a review_decision event."""
    event = {
        "proposal_id": record.get("proposal_id"),
        "type": EVENT_REVIEW_DECISION,
        "decision": record.get("decision"),
        "actor": record.get("decided_by"),
        "at": record.get("decided_at"),
    }
    for key in ("rework_note", "eval_run_id"):
        if record.get(key) is not None:
            event[key] = record[key]
    return event


def read_promotion_events(retro_dir: Path) -> list[dict]:
    """Read ``promotions.json`` as a normalized event list (any known format)."""
    path = retro_dir / "promotions.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict) and isinstance(raw.get("events"), list):
        return [event for event in raw["events"] if isinstance(event, dict)]
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = raw.get("promotions", [])
    else:
        return []
    if not isinstance(records, list):
        return []
    return [_legacy_record_to_event(record) for record in records if isinstance(record, dict)]


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def append_promotion_events(retro_dir: Path, events: list[dict]) -> list[dict]:
    """Append ``events`` to the stream and atomically rewrite ``promotions.json``.

    Returns the full event list after the append. Legacy-format files are
    upgraded to schema_version "2" on first write (old records are preserved
    as coerced ``review_decision`` events).
    """
    stream = read_promotion_events(retro_dir)
    stream.extend(events)
    _atomic_write(
        retro_dir / "promotions.json",
        {"schema_version": SCHEMA_VERSION, "events": stream},
    )
    return stream


def review_decision_event(
    proposal_id: str,
    *,
    decision: str,
    actor: str,
    at: str | None = None,
    rework_note: str | None = None,
) -> dict:
    event = {
        "proposal_id": proposal_id,
        "type": EVENT_REVIEW_DECISION,
        "decision": decision,
        "actor": actor,
        "at": at or utc_now_iso(),
    }
    if rework_note:
        event["rework_note"] = rework_note
    return event


def eval_completed_event(
    proposal_id: str,
    *,
    result: str,
    actor: str,
    at: str | None = None,
    run_ids: list[str] | None = None,
    gate: str | None = None,
    note: str | None = None,
) -> dict:
    event = {
        "proposal_id": proposal_id,
        "type": EVENT_EVAL_COMPLETED,
        "result": result,
        "run_ids": list(run_ids or []),
        "actor": actor,
        "at": at or utc_now_iso(),
    }
    if gate:
        event["gate"] = gate
    if note:
        event["note"] = note
    return event


def application_event(
    proposal_id: str,
    *,
    result: str,
    actor: str,
    at: str | None = None,
    target: str | None = None,
    content_sha256: str | None = None,
    note: str | None = None,
) -> dict:
    event = {
        "proposal_id": proposal_id,
        "type": EVENT_APPLICATION,
        "result": result,
        "actor": actor,
        "at": at or utc_now_iso(),
    }
    if target:
        event["target"] = target
    if content_sha256:
        event["content_sha256"] = content_sha256
    if note:
        event["note"] = note
    return event


def proposal_states(events: list[dict]) -> dict[str, str]:
    """Fold the event stream into the current state of each proposal."""
    states: dict[str, str] = {}
    for event in events:
        proposal_id = event.get("proposal_id")
        if not proposal_id:
            continue
        event_type = event.get("type")
        if event_type == EVENT_REVIEW_DECISION:
            decision = event.get("decision")
            if decision == "promoted":
                states[proposal_id] = "promoted_pending_eval"
            elif decision == "rejected":
                states[proposal_id] = "rejected"
            elif decision == "needs_rework":
                states[proposal_id] = "needs_rework"
        elif event_type == EVENT_EVAL_COMPLETED:
            result = event.get("result")
            if result == "pass":
                # Application event (or a retry on the next resume) follows.
                states[proposal_id] = "promoted_pending_eval"
            elif result == "regression":
                states[proposal_id] = "rolled_back"
            elif result == "inconclusive":
                states[proposal_id] = "awaiting_baseline"
            elif result == "error":
                states[proposal_id] = "eval_error"
        elif event_type == EVENT_APPLICATION:
            result = event.get("result")
            if result == "applied":
                states[proposal_id] = "applied"
            elif result == "rolled_back":
                states[proposal_id] = "rolled_back"
    return states


def effective_review_decisions(events: list[dict]) -> dict[str, dict]:
    """Latest ``review_decision`` event per proposal id."""
    decisions: dict[str, dict] = {}
    for event in events:
        if event.get("type") == EVENT_REVIEW_DECISION and event.get("proposal_id"):
            decisions[event["proposal_id"]] = event
    return decisions
