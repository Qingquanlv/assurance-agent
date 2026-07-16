import json
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import (
    EventWriteError,
    HealingAttemptAllocatedEvent,
    append_event_best_effort,
    append_event_strict,
    next_seq,
    read_events,
)


def _allocation(operation_id: str = "op-1") -> HealingAttemptAllocatedEvent:
    return HealingAttemptAllocatedEvent(
        episode_id="episode-1",
        attempt_id="attempt-1",
        attempt_number=1,
        operation_id=operation_id,
        source_batch_id="batch-1",
    )


def test_strict_appends_seq_and_ts(tmp_path: Path):
    append_event_strict(tmp_path, _allocation("op-1"))
    append_event_strict(tmp_path, _allocation("op-2"))
    evs = read_events(tmp_path)
    assert [e["seq"] for e in evs] == [1, 2]
    assert all("ts" in e for e in evs)
    assert next_seq(tmp_path) == 3


def test_strict_fails_when_dir_missing(tmp_path: Path):
    missing = tmp_path / "no-such-change"
    with pytest.raises(EventWriteError):
        append_event_strict(missing, _allocation())


def test_strict_rejects_missing_idempotency_key(tmp_path: Path):
    with pytest.raises(EventWriteError, match="operation_id"):
        append_event_strict(
            tmp_path,
            {
                "source": "progression",
                "type": "healing_attempt_allocated",
                "episode_id": "e",
                "attempt_id": "a",
                "attempt_number": 1,
                "source_batch_id": "b",
            },
        )


def test_best_effort_swallows_error(tmp_path: Path, capsys):
    missing = tmp_path / "no-such-change"
    append_event_best_effort(missing, {"type": "status_query"})  # 不抛
    assert read_events(missing) == []
    append_event_best_effort(tmp_path, {"type": "status_query", "bad": {1, 2}})  # TypeError 亦不抛


def test_read_skips_corrupt_and_non_object_lines(tmp_path: Path):
    append_event_strict(tmp_path, _allocation())
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write("42\n")
        fh.write("[1, 2]\n")
    evs = read_events(tmp_path)
    assert [e["type"] for e in evs] == ["healing_attempt_allocated"]
    assert next_seq(tmp_path) == 2


AUDIT_FIXTURES = [
    {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": "g",
        "action": "stop",
        "reason": "r",
        "who": "u",
    },
    {
        "source": "progression",
        "type": "dispatch_signed",
        "phase": "inspect",
        "kind": "dispatch_phase",
        "attempt_id": "a",
        "state_guard": "s",
        "dispatched_at": 1,
    },
    {
        "source": "progression",
        "type": "phase_outcome_committed",
        "phase": "inspect",
        "attempt_id": "a",
        "gate_report": None,
    },
    {
        "source": "progression",
        "type": "healing_attempt_allocated",
        "episode_id": "e",
        "attempt_id": "ha",
        "attempt_number": 1,
        "operation_id": "op",
        "source_batch_id": "b",
    },
    {
        "source": "heal",
        "type": "heal_record_apply",
        "target": "api",
        "proposal_sha256": "p",
        "source_batch_id": "b",
        "attempt_key": "p:b",
        "summary_sha256": "s",
        "files_modified": [],
    },
    {"source": "status", "type": "heal_transition", "from": "pending", "to": "failed"},
    {
        "source": "heal",
        "type": "healing_entry_baseline_pinned",
        "artifact_file": "healing/entry-baseline.json",
        "artifact_sha256": "x",
        "entry_batch_id": "b",
        "episode_id": "e",
    },
    {
        "source": "gate",
        "type": "gate_verdict",
        "phase": "case-review",
        "gate": "case-review-gate",
        "verdict": "pass",
        "reads_sha256": {"review/case-review.json": "abc"},
    },
    {
        "source": "report",
        "type": "failure_reclassified",
        "failure": "FAIL-001",
        "from": "test_data_failure",
        "to": "assertion_failure",
        "evidence": "fixture seeded ok",
    },
]


@pytest.mark.parametrize("payload", AUDIT_FIXTURES)
def test_every_frozen_audit_shape_serializes(tmp_path: Path, payload: dict):
    append_event_strict(tmp_path, payload)
    assert read_events(tmp_path)[0]["type"] == payload["type"]


def test_events_are_jsonl(tmp_path: Path):
    append_event_strict(tmp_path, _allocation())
    line = (tmp_path / "events.jsonl").read_text().strip()
    assert json.loads(line)["operation_id"] == "op-1"
