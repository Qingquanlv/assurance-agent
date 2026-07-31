import json
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import (
    EventWriteError,
    HealingAttemptAllocatedEvent,
    LedgerIntegrityError,
    append_event_best_effort,
    append_event_strict,
    next_seq,
    read_events,
    read_events_strict,
)
from assurance_agent.workflow.core.migrate_events import migrate_graph_event_stream


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


def _invocation_started_payload(invocation_id: str = "i") -> dict:
    return {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": invocation_id,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "d",
        "contract_digests": {},
        "params": {},
        "params_sha256": "p",
        "root_tree_id": "t",
        "max_parallel_tasks": 2,
        "checkpoint_ns": invocation_id,
        "structural_path": "main",
    }


def test_read_events_strict_rejects_bad_json_and_sequence_gap(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    (change / "events.jsonl").write_text(
        '{"seq":1,"ts":"x","source":"graph","type":"graph_invocation_started",'
        '"invocation_id":"i","entrypoint":"full","graph_id":"main",'
        '"graph_digest":"d","contract_digests":{},"params":{},'
        '"params_sha256":"p","root_tree_id":"t","max_parallel_tasks":2,'
        '"checkpoint_ns":"i","structural_path":"main"}\n'
        "{bad}\n",
        encoding="utf-8",
    )
    with pytest.raises(LedgerIntegrityError, match="line 2"):
        read_events_strict(change)


def test_read_events_strict_rejects_sequence_gap(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    append_event_strict(change, _invocation_started_payload())
    append_event_strict(change, _invocation_started_payload("j"))
    # 手工把第二行 seq 改成 3，制造 1..N 缺口。
    lines = (change / "events.jsonl").read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[1])
    forged["seq"] = 3
    lines[1] = json.dumps(forged, ensure_ascii=False)
    (change / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="line 2.*seq"):
        read_events_strict(change)


def test_read_events_strict_rejects_unknown_type_and_extra_field(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    unknown = dict(_invocation_started_payload())
    unknown["type"] = "graph_bogus"
    append_event_strict(change, _invocation_started_payload())  # seq 1 合法
    with (change / "events.jsonl").open("a", encoding="utf-8") as fh:
        record = {"seq": 2, "ts": "x", **unknown}
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    with pytest.raises(LedgerIntegrityError, match="line 2"):
        read_events_strict(change)

    change2 = tmp_path / "CH-2"
    change2.mkdir()
    append_event_strict(change2, _invocation_started_payload())
    with (change2 / "events.jsonl").open("a", encoding="utf-8") as fh:
        record = {"seq": 2, "ts": "x", **_invocation_started_payload("j"), "unexpected": 1}
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    with pytest.raises(LedgerIntegrityError, match="line 2"):
        read_events_strict(change2)


def test_read_events_strict_returns_envelopes_intact(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    append_event_strict(change, _invocation_started_payload())
    append_event_strict(change, _invocation_started_payload("j"))
    events = read_events_strict(change)
    assert [e["seq"] for e in events] == [1, 2]
    assert all(isinstance(e["ts"], str) and e["ts"] for e in events)
    assert [e["invocation_id"] for e in events] == ["i", "j"]


def test_graph_event_requires_declared_fields(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(EventWriteError):
        append_event_strict(
            change,
            {"source": "graph", "type": "task_attempt_started", "task_id": "missing-fields"},
        )


def test_migration_accepts_v4_and_rejects_future_versions() -> None:
    v4 = {
        "type": "graph_invocation_started",
        "invocation_id": "inv",
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "d",
        "event_schema_version": 4,
        "ir_digest": "d",
        "ingest_catalog_digest": "cat",
        "contract_digests": {},
        "policy_digest": "a" * 64,
        "policy_origin": "project",
        "gate_semantics_digest": "b" * 64,
        "assurance_profile_digest": "c" * 64,
        "params": {},
        "params_sha256": "p",
        "root_tree_id": "t",
        "max_parallel_tasks": 1,
        "checkpoint_ns": "inv",
        "structural_path": "main",
    }
    migrated = migrate_graph_event_stream([v4])
    assert migrated[0]["event_schema_version"] == 4
    assert migrated[0]["policy_origin"] == "project"

    future: dict[str, object] = dict(v4, event_schema_version=5)
    with pytest.raises(ValueError, match="unsupported graph event_schema_version 5"):
        migrate_graph_event_stream([future])


def test_migration_backfills_empty_binding_fields_for_legacy_versions() -> None:
    legacy = {
        "type": "graph_invocation_started",
        "invocation_id": "inv",
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "d",
        "event_schema_version": 2,
        "contract_digests": {},
        "params": {},
        "params_sha256": "p",
        "root_tree_id": "t",
        "max_parallel_tasks": 1,
        "checkpoint_ns": "inv",
        "structural_path": "main",
    }
    migrated = migrate_graph_event_stream([legacy])[0]
    assert migrated["policy_origin"] == ""
    assert migrated["gate_semantics_digest"] == ""
    assert migrated["assurance_profile_digest"] == ""
