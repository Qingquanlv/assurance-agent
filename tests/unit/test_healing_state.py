from pathlib import Path

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def test_attempts_count_unique_allocations_in_current_episode(tmp_path: Path):
    _event(
        tmp_path,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": "x",
            "entry_batch_id": "b1",
            "episode_id": "e1",
        },
    )
    allocation = {
        "source": "progression",
        "type": "healing_attempt_allocated",
        "episode_id": "e1",
        "attempt_id": "a1",
        "attempt_number": 1,
        "operation_id": "op1",
        "source_batch_id": "b1",
    }
    _event(tmp_path, allocation)
    _event(tmp_path, allocation)  # replayed ledger line does not consume another attempt
    assert derive_healing_state(tmp_path).attempts_used == 1


def test_apply_without_allocation_does_not_consume_budget(tmp_path: Path):
    _event(
        tmp_path,
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
    )
    assert derive_healing_state(tmp_path).attempts_used == 0


def test_derive_preserves_exhausted_after_baseline(tmp_path: Path):
    """Terminal heal_transition must keep its real verdict (not fold into not_needed)."""
    _event(
        tmp_path,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": "x",
            "entry_batch_id": "b1",
            "episode_id": "e1",
        },
    )
    _event(
        tmp_path,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": "e1",
            "attempt_id": "a1",
            "attempt_number": 1,
            "operation_id": "op1",
            "source_batch_id": "b1",
        },
    )
    _event(
        tmp_path,
        {
            "source": "status",
            "type": "heal_transition",
            "from": "pending",
            "to": "exhausted",
        },
    )
    snap = derive_healing_state(tmp_path)
    assert snap.status == "exhausted"
    assert snap.attempts_used == 1


def test_derive_preserves_failed_without_baseline(tmp_path: Path):
    """Pre-allocation terminal (e.g. allocate_on false) still surfaces via heal_transition."""
    _event(
        tmp_path,
        {
            "source": "status",
            "type": "heal_transition",
            "from": "pending",
            "to": "failed",
        },
    )
    assert derive_healing_state(tmp_path).status == "failed"
