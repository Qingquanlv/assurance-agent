"""Progression transaction: apply order, rollback, locks, path rules."""

from __future__ import annotations

import multiprocessing as mp
import signal
import threading
import time
from pathlib import Path

import pytest

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core import progression as progression_mod
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.core.progression import (
    ProgressionCommitError,
    ProgressionLockTimeout,
    ProgressionRollbackError,
    transaction,
)
from assurance_agent.workflow.core.state import read_state, write_state


def _event(operation_id: str = "op-1") -> dict:
    return {
        "source": "progression",
        "type": "healing_attempt_allocated",
        "episode_id": "ep",
        "attempt_id": "a1",
        "attempt_number": 1,
        "operation_id": operation_id,
        "source_batch_id": "b1",
    }


def test_apply_order_files_then_events_then_state(tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())

    real_atomic = progression_mod._atomic_write_bytes
    real_append = progression_mod.append_event_strict
    real_write_state = progression_mod.write_state

    def track_file(path: Path, data: bytes) -> None:
        order.append(f"file:{path.name}")
        real_atomic(path, data)

    def track_append(change_dir: Path, event) -> None:  # noqa: ANN001
        order.append("event")
        real_append(change_dir, event)

    def track_state(change_dir: Path, state: WorkflowState) -> None:
        order.append("state")
        real_write_state(change_dir, state)

    monkeypatch.setattr(progression_mod, "_atomic_write_bytes", track_file)
    monkeypatch.setattr(progression_mod, "append_event_strict", track_append)
    monkeypatch.setattr(progression_mod, "write_state", track_state)

    with transaction(change) as txn:
        txn.write_file("healing/note.txt", "hi")
        txn.append_strict(_event())
        txn.set_state(WorkflowState.model_validate({"phases": {"explore": {"status": "done"}}}))

    assert order == ["file:note.txt", "event", "state"]
    assert (change / "healing" / "note.txt").read_text() == "hi"
    assert read_events(change)[0]["type"] == "healing_attempt_allocated"
    explore = (read_state(change).phases.model_extra or {})["explore"]
    assert explore["status"] == "done"


def test_txn_ledger_reads_committed_events(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())

    with transaction(change) as txn:
        txn.append_strict(_event(operation_id="op-ledger"))
        # Staged events are not visible via ledger until commit.
        assert txn.ledger.latest(type="healing_attempt_allocated") is None

    with transaction(change) as txn:
        latest = txn.ledger.latest(type="healing_attempt_allocated")
        assert latest is not None
        assert latest["operation_id"] == "op-ledger"
        assert txn.ledger.filter(type="healing_attempt_allocated") == [latest]


def test_block_exception_writes_nothing(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())
    with pytest.raises(RuntimeError, match="boom"):
        with transaction(change) as txn:
            txn.write_file("healing/note.txt", "hi")
            txn.append_strict(_event())
            txn.set_state(WorkflowState.model_validate({"phases": {"a": {"status": "done"}}}))
            raise RuntimeError("boom")
    assert not (change / "healing" / "note.txt").exists()
    assert read_events(change) == []
    assert "explore" not in (read_state(change).phases.model_extra or {})


def test_commit_failure_restores_and_preserves_cause(tmp_path: Path, monkeypatch) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())
    (change / "healing").mkdir()
    (change / "healing" / "note.txt").write_text("original", encoding="utf-8")

    def fail_append(*_a, **_k) -> None:
        raise OSError("append failed")

    monkeypatch.setattr(progression_mod, "append_event_strict", fail_append)
    with pytest.raises(ProgressionCommitError) as ei:
        with transaction(change) as txn:
            txn.write_file("healing/note.txt", "new")
            txn.append_strict(_event())
    assert isinstance(ei.value.__cause__, OSError)
    assert (change / "healing" / "note.txt").read_text() == "original"
    assert read_events(change) == []


def test_rollback_failure_raises_dual_cause(tmp_path: Path, monkeypatch) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())

    def fail_append(*_a, **_k) -> None:
        raise OSError("append failed")

    def fail_restore(*_a, **_k) -> None:
        raise RuntimeError("restore failed")

    monkeypatch.setattr(progression_mod, "append_event_strict", fail_append)
    monkeypatch.setattr(progression_mod, "restore_files", fail_restore)
    with pytest.raises(ProgressionRollbackError) as ei:
        with transaction(change) as txn:
            txn.write_file("healing/note.txt", "x")
            txn.append_strict(_event())
    assert isinstance(ei.value.commit_cause, OSError)
    assert isinstance(ei.value.rollback_cause, RuntimeError)
    assert ei.value.affected


def test_set_state_twice_raises(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(ValueError, match="at most once"):
        with transaction(change) as txn:
            txn.set_state(WorkflowState())
            txn.set_state(WorkflowState())


def test_set_workflow_state_projection_writes_reserved_file(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with transaction(change) as txn:
        txn.set_workflow_state_projection(b"invocation_id: inv-1\n")
    assert (change / "workflow-state.yaml").read_bytes() == b"invocation_id: inv-1\n"


def test_set_workflow_state_projection_accepts_str(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with transaction(change) as txn:
        txn.set_workflow_state_projection("terminal: completed\n")
    assert (change / "workflow-state.yaml").read_bytes() == b"terminal: completed\n"


def test_set_workflow_state_projection_rejects_second_call(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(ValueError, match="at most once"):
        with transaction(change) as txn:
            txn.set_workflow_state_projection(b"a")
            txn.set_workflow_state_projection(b"b")


def test_set_workflow_state_projection_rejects_set_state_mix(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(ValueError, match="mixed"):
        with transaction(change) as txn:
            txn.set_state(WorkflowState())
            txn.set_workflow_state_projection(b"a")
    with pytest.raises(ValueError, match="mixed"):
        with transaction(change) as txn:
            txn.set_workflow_state_projection(b"a")
            txn.set_state(WorkflowState())


def test_workflow_state_projection_block_exception_writes_nothing(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(RuntimeError, match="boom"):
        with transaction(change) as txn:
            txn.set_workflow_state_projection(b"x")
            raise RuntimeError("boom")
    assert not (change / "workflow-state.yaml").exists()


def test_workflow_state_projection_rolls_back_on_commit_failure(tmp_path: Path, monkeypatch) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())
    original = (change / "workflow-state.yaml").read_bytes()

    def fail_append(*_a, **_k) -> None:
        raise OSError("append failed")

    monkeypatch.setattr(progression_mod, "append_event_strict", fail_append)
    with pytest.raises(ProgressionCommitError):
        with transaction(change) as txn:
            txn.set_workflow_state_projection(b"projected: true\n")
            txn.append_strict(_event())
    # 投影字节随事务回滚，磁盘保留原 workflow-state.yaml。
    assert (change / "workflow-state.yaml").read_bytes() == original


def test_empty_transaction_is_noop(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())
    before = (change / "workflow-state.yaml").read_bytes()
    with transaction(change):
        pass
    assert (change / "workflow-state.yaml").read_bytes() == before
    assert read_events(change) == []


@pytest.mark.parametrize(
    "rel",
    ["events.jsonl", "workflow-state.yaml", ".progression.lock", "../outside.txt", "/abs.txt"],
)
def test_write_file_rejects_reserved_and_escape(tmp_path: Path, rel: str) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(ValueError):
        with transaction(change) as txn:
            txn.write_file(rel, b"x")


def test_thread_lock_serializes_rmw(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    write_state(change, WorkflowState())
    errors: list[BaseException] = []

    def worker(op_id: str) -> None:
        try:
            with transaction(change, lock_timeout_s=2.0) as txn:
                n = len(txn.read_events())
                time.sleep(0.05)
                txn.append_strict(_event(op_id))
                assert len(txn.read_events()) == n  # staged only until exit
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("op-a",))
    t2 = threading.Thread(target=worker, args=("op-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    assert {e["operation_id"] for e in read_events(change)} == {"op-a", "op-b"}


def _hold_lock(change_str: str, ready: object, release: object) -> None:
    change = Path(change_str)
    with transaction(change, lock_timeout_s=5.0):
        ready.set()  # type: ignore[attr-defined]
        release.wait(timeout=5.0)  # type: ignore[attr-defined]


def test_process_lock_timeout(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    ready = mp.Event()
    release = mp.Event()
    proc = mp.Process(target=_hold_lock, args=(str(change), ready, release))
    proc.start()
    assert ready.wait(timeout=5.0)
    with pytest.raises(ProgressionLockTimeout):
        with transaction(change, lock_timeout_s=0.15):
            pass
    release.set()
    proc.join(timeout=5.0)
    assert proc.exitcode == 0


def _acquire_then_die(change_str: str, ready: object) -> None:
    change = Path(change_str)
    # Enter transaction and hold flock, then hard-exit without unlock.
    import fcntl
    import os as _os

    change.mkdir(parents=True, exist_ok=True)
    fd = _os.open(str(change / ".progression.lock"), _os.O_RDWR | _os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()  # type: ignore[attr-defined]
    time.sleep(0.2)
    _os.kill(_os.getpid(), signal.SIGKILL)


def test_killed_holder_releases_fcntl_lock(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    ready = mp.Event()
    proc = mp.Process(target=_acquire_then_die, args=(str(change), ready))
    proc.start()
    assert ready.wait(timeout=5.0)
    proc.join(timeout=5.0)
    # Kernel should have released the dead process's flock.
    with transaction(change, lock_timeout_s=1.0) as txn:
        txn.write_file("healing/ok.txt", "yes")
    assert (change / "healing" / "ok.txt").read_text() == "yes"
