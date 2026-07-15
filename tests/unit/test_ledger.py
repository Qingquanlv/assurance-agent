from pathlib import Path

from assurance_agent.workflow.core.events import (
    HealingAttemptAllocatedEvent,
    Ledger,
    append_event_best_effort,
    append_event_strict,
    event_seq,
)


def _allocation(operation_id: str = "op-1", *, episode_id: str = "ep-1") -> HealingAttemptAllocatedEvent:
    return HealingAttemptAllocatedEvent(
        episode_id=episode_id,
        attempt_id="attempt-1",
        attempt_number=1,
        operation_id=operation_id,
        source_batch_id="batch-1",
    )


def test_event_seq_reads_int_or_zero() -> None:
    assert event_seq({"seq": 3}) == 3
    assert event_seq({}) == 0
    assert event_seq({"seq": "x"}) == 0


def test_ledger_all_and_filter_by_type(tmp_path: Path) -> None:
    append_event_strict(tmp_path, _allocation("op-1"))
    append_event_best_effort(tmp_path, {"type": "driver_started", "run_id": "r1"})
    append_event_strict(tmp_path, _allocation("op-2"))

    ledger = Ledger(tmp_path)
    assert len(ledger.all()) == 3
    allocs = ledger.filter(type="healing_attempt_allocated")
    assert [e["operation_id"] for e in allocs] == ["op-1", "op-2"]
    assert ledger.filter(type="driver_started")[0]["run_id"] == "r1"


def test_ledger_filter_attrs_and_after_seq(tmp_path: Path) -> None:
    append_event_strict(tmp_path, _allocation("op-1", episode_id="ep-a"))
    append_event_strict(tmp_path, _allocation("op-2", episode_id="ep-b"))
    append_event_strict(tmp_path, _allocation("op-3", episode_id="ep-a"))

    ledger = Ledger(tmp_path)
    assert [e["operation_id"] for e in ledger.filter(type="healing_attempt_allocated", episode_id="ep-a")] == [
        "op-1",
        "op-3",
    ]
    assert [e["operation_id"] for e in ledger.filter(type="healing_attempt_allocated", after_seq=1)] == [
        "op-2",
        "op-3",
    ]


def test_ledger_latest_returns_max_seq_or_none(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    assert ledger.latest(type="healing_attempt_allocated") is None

    append_event_strict(tmp_path, _allocation("op-1"))
    append_event_strict(tmp_path, _allocation("op-2"))
    latest = ledger.latest(type="healing_attempt_allocated")
    assert latest is not None
    assert latest["operation_id"] == "op-2"
    assert event_seq(latest) == 2


def test_ledger_latest_with_attrs(tmp_path: Path) -> None:
    append_event_strict(
        tmp_path,
        {
            "source": "progression",
            "type": "dispatch_signed",
            "phase": "inspect",
            "kind": "dispatch_phase",
            "attempt_id": "a1",
            "state_guard": "g1",
            "dispatched_at": 1,
        },
    )
    append_event_strict(
        tmp_path,
        {
            "source": "progression",
            "type": "dispatch_signed",
            "phase": "inspect",
            "kind": "dispatch_phase",
            "attempt_id": "a2",
            "state_guard": "g2",
            "dispatched_at": 2,
        },
    )
    ledger = Ledger(tmp_path)
    hit = ledger.latest(type="dispatch_signed", attempt_id="a1")
    assert hit is not None
    assert hit["state_guard"] == "g1"
    assert ledger.latest(type="dispatch_signed", attempt_id="missing") is None
