from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.runtime.task_workspace import TaskWorkspaceStore, TaskWorkspaceViolation


def test_partial_promotion_failure_leaves_a_durable_pending_record_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("left.txt", "right.txt"))
    (binding.write_root / "left.txt").write_bytes(b"left")
    (binding.write_root / "right.txt").write_bytes(b"right")
    staged = store.seal(binding.identity)

    real_replace = __import__("os").replace
    calls = 0

    def fail_second_replace(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected second replace failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", fail_second_replace)

    with pytest.raises(OSError, match="injected"):
        store.promote(binding.identity, staged)

    assert (project / "left.txt").read_bytes() == b"left"
    assert not (project / "right.txt").exists()
    assert (store.receipts_root / f".{binding.identity.identity_digest}.pending.json").is_file()
    receipt = store.promote(binding.identity, staged)
    assert (project / "right.txt").read_bytes() == b"right"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()


def test_identical_retry_completes_remaining_targets_after_second_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("left.txt", "right.txt"))
    (binding.write_root / "left.txt").write_bytes(b"left")
    (binding.write_root / "right.txt").write_bytes(b"right")
    staged = store.seal(binding.identity)
    real_replace = __import__("os").replace
    calls = 0

    def fail_once_on_second_target(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        nonlocal calls
        if target == "right.txt" and "dst_dir_fd" in kwargs:
            calls += 1
            if calls == 1:
                raise OSError("injected second target failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", fail_once_on_second_target)

    with pytest.raises(OSError, match="injected second target"):
        store.promote(binding.identity, staged)

    receipt = store.promote(binding.identity, staged)

    assert (project / "left.txt").read_bytes() == b"left"
    assert (project / "right.txt").read_bytes() == b"right"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()


def test_pending_retry_rejects_a_third_target_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("left.txt", "right.txt"))
    (binding.write_root / "left.txt").write_bytes(b"left")
    (binding.write_root / "right.txt").write_bytes(b"right")
    staged = store.seal(binding.identity)
    real_replace = __import__("os").replace

    def fail_second_target(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        if target == "right.txt" and "dst_dir_fd" in kwargs:
            raise OSError("injected second target failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", fail_second_target)
    with pytest.raises(OSError, match="injected second target"):
        store.promote(binding.identity, staged)
    monkeypatch.undo()
    (project / "right.txt").write_bytes(b"third state")

    with pytest.raises(TaskWorkspaceViolation, match="incomplete"):
        store.promote(binding.identity, staged)


def test_target_changed_immediately_before_replace_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out.txt").write_bytes(b"before")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    real_replace = store._replace_file

    def change_target_before_replace(*args: object, **kwargs: object) -> None:
        (project / "out.txt").write_bytes(b"drifted")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(store, "_replace_file", change_target_before_replace)

    with pytest.raises(TaskWorkspaceViolation, match="target drift"):
        store.promote(binding.identity, staged)
    assert (project / "out.txt").read_bytes() == b"drifted"
