from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import graph_engine.runtime.task_workspace as task_workspace
from graph_engine.runtime.task_workspace import TaskWorkspaceStore, TaskWorkspaceViolation


def test_second_file_replace_failure_rolls_back_the_whole_promotion_and_can_replay(
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

    def fail_second_replace(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        if target == "right.txt" and "dst_dir_fd" in kwargs:
            raise OSError("injected second replace failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", fail_second_replace)

    with pytest.raises(OSError, match="injected"):
        store.promote(binding.identity, staged)

    assert not (project / "left.txt").exists()
    assert not (project / "right.txt").exists()
    assert (store.receipts_root / f".{binding.identity.identity_digest}.pending.json").is_file()
    monkeypatch.undo()
    receipt = store.promote(binding.identity, staged)
    assert (project / "left.txt").read_bytes() == b"left"
    assert (project / "right.txt").read_bytes() == b"right"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_temp_preparation_failure_cleans_adjacent_files_before_canonical_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out.txt").write_bytes(b"before")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    real_open = task_workspace.os.open
    real_write = task_workspace.os.write
    real_fsync = task_workspace.os.fsync
    target_fds: set[int] = set()

    def track_target_temp(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        if isinstance(name, str) and name.startswith(".out.txt.") and "dir_fd" in kwargs:
            target_fds.add(descriptor)
        return descriptor

    def fail_target_write(descriptor: int, content: object) -> int:
        if failure == "write" and descriptor in target_fds:
            raise OSError("injected target temp write failure")
        return real_write(descriptor, content)  # type: ignore[arg-type]

    def fail_target_fsync(descriptor: int) -> None:
        if failure == "fsync" and descriptor in target_fds:
            raise OSError("injected target temp fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(task_workspace.os, "open", track_target_temp)
    monkeypatch.setattr(task_workspace.os, "write", fail_target_write)
    monkeypatch.setattr(task_workspace.os, "fsync", fail_target_fsync)

    with pytest.raises(OSError, match=f"injected target temp {failure} failure"):
        store.promote(binding.identity, staged)

    assert (project / "out.txt").read_bytes() == b"before"
    assert tuple(project.glob(".out.txt.*.tmp")) == ()


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


def test_completed_receipt_replay_removes_a_stale_pending_journal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    receipt = store.promote(binding.identity, staged)
    receipt_path = store.receipts_root / f"{receipt.identity_digest}.json"
    pending_path = store.receipts_root / f".{receipt.identity_digest}.pending.json"
    pending_path.write_bytes(receipt_path.read_bytes())

    assert store.promote(binding.identity, staged) == receipt

    assert not pending_path.exists()


def test_staged_digest_and_promotion_bind_and_apply_file_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("tool.sh",))
    staged_path = binding.write_root / "tool.sh"
    staged_path.write_bytes(b"#!/bin/sh\n")
    staged_path.chmod(0o750)
    executable = store.seal(binding.identity)
    staged_path.chmod(0o640)
    non_executable = store.seal(binding.identity)

    assert executable.staged_digest != non_executable.staged_digest
    assert executable.files[0].after_mode == 0o750
    receipt = store.promote(binding.identity, non_executable)

    assert receipt.staged_digest == non_executable.staged_digest
    assert stat.S_IMODE((project / "tool.sh").stat().st_mode) == 0o640


def test_mode_application_failure_cleans_temp_and_leaves_canonical_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "tool.sh").write_bytes(b"before")
    (project / "tool.sh").chmod(0o644)
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("tool.sh",))
    (binding.write_root / "tool.sh").write_bytes(b"after")
    (binding.write_root / "tool.sh").chmod(0o750)
    staged = store.seal(binding.identity)
    real_open = task_workspace.os.open
    real_fchmod = os.fchmod
    target_fds: set[int] = set()

    def track_target_temp(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        if isinstance(name, str) and name.startswith(".tool.sh.") and "dir_fd" in kwargs:
            target_fds.add(descriptor)
        return descriptor

    def fail_target_mode(descriptor: int, mode: int) -> None:
        if descriptor in target_fds:
            raise OSError("injected target temp mode failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(task_workspace.os, "open", track_target_temp)
    monkeypatch.setattr(task_workspace.os, "fchmod", fail_target_mode)

    with pytest.raises(OSError, match="injected target temp mode failure"):
        store.promote(binding.identity, staged)

    assert (project / "tool.sh").read_bytes() == b"before"
    assert stat.S_IMODE((project / "tool.sh").stat().st_mode) == 0o644
    assert tuple(project.glob(".tool.sh.*.tmp")) == ()


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
    real_transaction = store._execute_promotion_transaction

    def change_target_before_replace(*args: object, **kwargs: object) -> None:
        (project / "out.txt").write_bytes(b"drifted")
        real_transaction(*args, **kwargs)

    monkeypatch.setattr(store, "_execute_promotion_transaction", change_target_before_replace)

    with pytest.raises(TaskWorkspaceViolation, match="target drift"):
        store.promote(binding.identity, staged)
    assert (project / "out.txt").read_bytes() == b"drifted"


def test_target_changed_during_temp_preparation_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out.txt").write_bytes(b"before")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    real_open = task_workspace.os.open
    real_fsync = task_workspace.os.fsync
    temporary_descriptors: set[int] = set()
    mutated = False

    def record_target_temporary(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        if isinstance(name, str) and name.startswith(".out.txt.") and "dir_fd" in kwargs:
            temporary_descriptors.add(descriptor)
        return descriptor

    def mutate_after_temp_fsync(descriptor: int) -> None:
        nonlocal mutated
        real_fsync(descriptor)
        if descriptor in temporary_descriptors and not mutated:
            mutated = True
            (project / "out.txt").write_bytes(b"drifted")

    monkeypatch.setattr(task_workspace.os, "open", record_target_temporary)
    monkeypatch.setattr(task_workspace.os, "fsync", mutate_after_temp_fsync)

    with pytest.raises(TaskWorkspaceViolation, match="target drift"):
        store.promote(binding.identity, staged)
    assert mutated
    assert (project / "out.txt").read_bytes() == b"drifted"
    assert not (store.receipts_root / f"{binding.identity.identity_digest}.json").exists()
