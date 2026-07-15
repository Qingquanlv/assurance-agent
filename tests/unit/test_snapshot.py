from pathlib import Path

from assurance_agent.workflow.core.snapshot import capture_files, restore_files


def test_restore_reinstates_existing_and_removes_new_file(tmp_path: Path):
    state = tmp_path / "workflow-state.yaml"
    events = tmp_path / "events.jsonl"
    state.write_bytes(b"before-state")
    snapshots = capture_files([state, events])

    state.write_bytes(b"after-state")
    events.write_bytes(b"new-event\n")
    restore_files(snapshots)

    assert state.read_bytes() == b"before-state"
    assert not events.exists()


def test_capture_is_immutable_bytes_snapshot(tmp_path: Path):
    target = tmp_path / "x"
    target.write_bytes(b"v1")
    snapshots = capture_files([target])
    target.write_bytes(b"v2")
    assert snapshots[0].content == b"v1"
