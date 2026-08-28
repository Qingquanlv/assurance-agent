from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import graph_engine.runtime.task_workspace as task_workspace
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import StagedWriteSet, TaskWorkspaceBinding
from graph_engine.runtime.task_workspace import (
    PromotionPublicationIndeterminate,
    TaskWorkspaceStore,
    TaskWorkspaceViolation,
)


def _assert_publication_indeterminate(error: pytest.ExceptionInfo[BaseException]) -> None:
    assert isinstance(error.value, PromotionPublicationIndeterminate)


def _single_file_promotion(
    tmp_path: Path,
) -> tuple[TaskWorkspaceStore, TaskWorkspaceBinding, StagedWriteSet, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out.txt").write_bytes(b"before")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    return store, binding, staged, project


def _crash_while_writing_receipt_file(
    *,
    project: Path,
    attempts: Path,
    receipts: Path,
    binding: TaskWorkspaceBinding,
    staged: StagedWriteSet,
    purpose: str,
) -> int:
    process_id = os.fork()
    if process_id == 0:
        child_store = TaskWorkspaceStore(project, attempts, receipts)
        real_open = task_workspace.os.open
        real_write = task_workspace.os.write
        target_descriptors: set[int] = set()

        def track_receipt_file(
            name: str | bytes | Path,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            descriptor = real_open(name, flags, *args, **kwargs)
            if (
                kwargs.get("dir_fd") == child_store._receipts_fd
                and isinstance(name, str)
                and binding.identity.identity_digest in name
                and purpose in name
                and name.endswith(".tmp")
            ):
                target_descriptors.add(descriptor)
            return descriptor

        def crash_after_partial_write(descriptor: int, content: object) -> int:
            if descriptor in target_descriptors:
                payload = memoryview(content)  # type: ignore[arg-type]
                written = real_write(descriptor, payload[: max(1, min(7, len(payload)))])
                if written > 0:
                    os._exit(91)
            return real_write(descriptor, content)  # type: ignore[arg-type]

        task_workspace.os.open = track_receipt_file
        task_workspace.os.write = crash_after_partial_write
        child_store.promote(binding.identity, staged)
        os._exit(92)
    _child, status = os.waitpid(process_id, 0)
    return os.waitstatus_to_exitcode(status)


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


@pytest.mark.parametrize("failure", ["write", "file_fsync", "rename", "directory_fsync"])
def test_receipt_publication_fault_after_canonical_replace_is_indeterminate_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    identity_digest = binding.identity.identity_digest
    receipt_name = f"{identity_digest}.json"
    pending_name = f".{identity_digest}.pending.json"
    real_open = task_workspace.os.open
    real_write = task_workspace.os.write
    real_fsync = task_workspace.os.fsync
    real_replace = task_workspace.os.replace
    canonical_replaced = False
    receipt_renamed = False
    receipt_temp_descriptors: set[int] = set()

    def track_open(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        if (
            canonical_replaced
            and kwargs.get("dir_fd") == store._receipts_fd
            and isinstance(name, str)
            and identity_digest in name
            and "pending" not in name
        ):
            receipt_temp_descriptors.add(descriptor)
        return descriptor

    def fail_write(descriptor: int, content: object) -> int:
        if failure == "write" and descriptor in receipt_temp_descriptors:
            raise OSError("injected receipt write failure")
        return real_write(descriptor, content)  # type: ignore[arg-type]

    def fail_fsync(descriptor: int) -> None:
        if failure == "file_fsync" and descriptor in receipt_temp_descriptors:
            raise OSError("injected receipt file fsync failure")
        if failure == "directory_fsync" and receipt_renamed and descriptor == store._receipts_fd:
            raise OSError("injected receipt directory fsync failure")
        real_fsync(descriptor)

    def fail_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal canonical_replaced, receipt_renamed
        if target == "out.txt":
            canonical_replaced = True
        if target == receipt_name and kwargs.get("dst_dir_fd") == store._receipts_fd:
            if failure == "rename":
                raise OSError("injected receipt rename failure")
            receipt_renamed = True
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "open", track_open)
    monkeypatch.setattr(task_workspace.os, "write", fail_write)
    monkeypatch.setattr(task_workspace.os, "fsync", fail_fsync)
    monkeypatch.setattr(task_workspace.os, "replace", fail_replace)

    with pytest.raises(BaseException) as caught:
        store.promote(binding.identity, staged)

    _assert_publication_indeterminate(caught)
    assert canonical_replaced
    assert (project / "out.txt").read_bytes() == b"after"
    assert (store.receipts_root / pending_name).is_file()
    monkeypatch.undo()

    receipt = store.promote(binding.identity, staged)

    assert receipt.identity_digest == identity_digest
    assert (store.receipts_root / receipt_name).is_file()
    assert not (store.receipts_root / pending_name).exists()
    assert tuple(store.receipts_root.glob("*.tmp")) == ()
    assert tuple(project.glob(".out.txt.*")) == ()


def test_receipt_rename_reuses_one_deterministic_authenticated_temp_until_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, binding, staged, _project = _single_file_promotion(tmp_path)
    identity_digest = binding.identity.identity_digest
    receipt_name = f"{identity_digest}.json"
    real_replace = task_workspace.os.replace

    def fail_receipt_rename(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == receipt_name and kwargs.get("dst_dir_fd") == store._receipts_fd:
            raise OSError("injected receipt rename failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "replace", fail_receipt_rename)
    with pytest.raises(BaseException) as first:
        store.promote(binding.identity, staged)
    _assert_publication_indeterminate(first)
    first_temps = tuple(store.receipts_root.glob("*.tmp"))
    assert len(first_temps) == 1

    with pytest.raises(BaseException) as second:
        store.promote(binding.identity, staged)
    _assert_publication_indeterminate(second)
    assert tuple(store.receipts_root.glob("*.tmp")) == first_temps

    monkeypatch.undo()
    store.promote(binding.identity, staged)
    assert tuple(store.receipts_root.glob("*.tmp")) == ()


def test_replay_replaces_unparseable_same_intent_receipt_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, binding, staged, _project = _single_file_promotion(tmp_path)
    receipt_name = f"{binding.identity.identity_digest}.json"
    real_replace = task_workspace.os.replace

    def fail_receipt_rename(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == receipt_name and kwargs.get("dst_dir_fd") == store._receipts_fd:
            raise OSError("injected receipt rename failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "replace", fail_receipt_rename)
    with pytest.raises(PromotionPublicationIndeterminate):
        store.promote(binding.identity, staged)
    (temporary,) = tuple(store.receipts_root.glob("*.tmp"))
    temporary.write_bytes(b"forged")
    monkeypatch.undo()

    receipt = store.promote(binding.identity, staged)

    assert (store.receipts_root / receipt_name).is_file()
    assert receipt.identity_digest == binding.identity.identity_digest


def test_replay_preserves_authenticated_different_intent_receipt_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, binding, staged, _project = _single_file_promotion(tmp_path)
    receipt_name = f"{binding.identity.identity_digest}.json"
    real_replace = task_workspace.os.replace

    def fail_receipt_rename(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == receipt_name and kwargs.get("dst_dir_fd") == store._receipts_fd:
            raise OSError("injected receipt rename failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "replace", fail_receipt_rename)
    with pytest.raises(PromotionPublicationIndeterminate):
        store.promote(binding.identity, staged)
    (temporary,) = tuple(store.receipts_root.glob("*.tmp"))
    different = task_workspace._receipt(binding.identity.identity_digest, "f" * 64)
    different_bytes = canonical_json_bytes(different.model_dump(mode="json"))
    temporary.write_bytes(different_bytes)
    temporary.chmod(0o600)
    monkeypatch.undo()

    with pytest.raises(PromotionPublicationIndeterminate):
        store.promote(binding.identity, staged)

    assert temporary.read_bytes() == different_bytes
    assert not (store.receipts_root / receipt_name).exists()


def test_pending_replay_preserves_authenticated_legacy_different_intent_construction(
    tmp_path: Path,
) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    different = task_workspace._receipt(binding.identity.identity_digest, "f" * 64)
    different_bytes = canonical_json_bytes(different.model_dump(mode="json"))
    legacy = store.receipts_root / f"..{binding.identity.identity_digest}.pending.json.{'a' * 32}.tmp"
    legacy.write_bytes(different_bytes)
    legacy.chmod(0o600)

    with pytest.raises(TaskWorkspaceViolation, match="different promotion intent"):
        store.promote(binding.identity, staged)

    assert legacy.read_bytes() == different_bytes
    assert (project / "out.txt").read_bytes() == b"before"


def test_pending_replay_cleans_partial_legacy_construction(tmp_path: Path) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    legacy = store.receipts_root / f"..{binding.identity.identity_digest}.pending.json.{'b' * 32}.tmp"
    legacy.write_bytes(b'{"partial"')
    legacy.chmod(0o600)

    receipt = store.promote(binding.identity, staged)

    assert not legacy.exists()
    assert (project / "out.txt").read_bytes() == b"after"
    assert {entry.name for entry in store.receipts_root.iterdir()} == {f"{receipt.identity_digest}.json"}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
def test_fresh_replays_replace_partial_completed_receipt_construction_and_finish(
    tmp_path: Path,
) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    attempts = store.attempts_root
    receipts = store.receipts_root
    store.close()

    assert (
        _crash_while_writing_receipt_file(
            project=project,
            attempts=attempts,
            receipts=receipts,
            binding=binding,
            staged=staged,
            purpose="receipt",
        )
        == 91
    )
    assert (project / "out.txt").read_bytes() == b"after"
    assert (receipts / f".{binding.identity.identity_digest}.pending.json").is_file()

    replayed = []
    for _ in range(2):
        fresh = TaskWorkspaceStore(project, attempts, receipts)
        try:
            replayed.append(fresh.promote(binding.identity, staged))
        finally:
            fresh.close()

    assert replayed[0] == replayed[1]
    assert {entry.name for entry in receipts.iterdir()} == {f"{binding.identity.identity_digest}.json"}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
def test_repeated_pending_intent_construction_crashes_do_not_accumulate_orphans(
    tmp_path: Path,
) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    attempts = store.attempts_root
    receipts = store.receipts_root
    store.close()

    for _ in range(2):
        assert (
            _crash_while_writing_receipt_file(
                project=project,
                attempts=attempts,
                receipts=receipts,
                binding=binding,
                staged=staged,
                purpose="pending",
            )
            == 91
        )

    pending_orphans = tuple(
        entry
        for entry in receipts.iterdir()
        if binding.identity.identity_digest in entry.name
        and "pending" in entry.name
        and entry.name.endswith(".tmp")
    )
    assert len(pending_orphans) == 1
    assert (project / "out.txt").read_bytes() == b"before"

    fresh = TaskWorkspaceStore(project, attempts, receipts)
    try:
        receipt = fresh.promote(binding.identity, staged)
    finally:
        fresh.close()

    assert (project / "out.txt").read_bytes() == b"after"
    assert {entry.name for entry in receipts.iterdir()} == {f"{receipt.identity_digest}.json"}


@pytest.mark.parametrize(
    "failure",
    ["transaction_unlink", "pending_unlink", "pending_directory_fsync"],
)
def test_durable_receipt_makes_cleanup_fault_nonfatal_and_replay_cleans_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store, binding, staged, project = _single_file_promotion(tmp_path)
    identity_digest = binding.identity.identity_digest
    receipt_path = store.receipts_root / f"{identity_digest}.json"
    pending_name = f".{identity_digest}.pending.json"
    pending_path = store.receipts_root / pending_name
    real_unlink = task_workspace.os.unlink
    real_fsync = task_workspace.os.fsync
    injected = False
    pending_unlinked = False

    def fail_cleanup_unlink(name: str | bytes | Path, *args: object, **kwargs: object) -> None:
        nonlocal injected, pending_unlinked
        if failure == "transaction_unlink" and isinstance(name, str) and name.endswith(".rollback"):
            assert receipt_path.is_file(), "transaction cleanup ran before durable receipt publication"
            injected = True
            raise OSError("injected transaction cleanup unlink failure")
        if failure == "pending_unlink" and name == pending_name:
            assert receipt_path.is_file()
            injected = True
            raise OSError("injected pending unlink failure")
        real_unlink(name, *args, **kwargs)
        if name == pending_name:
            pending_unlinked = True

    def fail_cleanup_fsync(descriptor: int) -> None:
        nonlocal injected
        if failure == "pending_directory_fsync" and pending_unlinked and descriptor == store._receipts_fd:
            injected = True
            raise OSError("injected pending directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(task_workspace.os, "unlink", fail_cleanup_unlink)
    monkeypatch.setattr(task_workspace.os, "fsync", fail_cleanup_fsync)

    receipt = store.promote(binding.identity, staged)

    assert injected
    assert receipt_path.is_file()
    assert (project / "out.txt").read_bytes() == b"after"
    monkeypatch.undo()
    assert store.promote(binding.identity, staged) == receipt
    assert not pending_path.exists()
    assert tuple(project.glob(".out.txt.*")) == ()


def test_rollback_failure_is_indeterminate_and_preserves_evidence_for_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "left.txt").write_bytes(b"left-before")
    (project / "right.txt").write_bytes(b"right-before")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("left.txt", "right.txt"))
    (binding.write_root / "left.txt").write_bytes(b"left-after")
    (binding.write_root / "right.txt").write_bytes(b"right-after")
    staged = store.seal(binding.identity)
    real_replace = task_workspace.os.replace

    def fail_second_replace_and_rollback(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == "right.txt":
            raise OSError("injected second replace failure")
        if target == "left.txt" and isinstance(source, str) and source.endswith(".rollback"):
            raise OSError("injected rollback failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "replace", fail_second_replace_and_rollback)

    with pytest.raises(BaseException) as caught:
        store.promote(binding.identity, staged)

    _assert_publication_indeterminate(caught)
    assert (project / "left.txt").read_bytes() == b"left-after"
    assert (project / "right.txt").read_bytes() == b"right-before"
    assert len(tuple(project.glob(".left.txt.*.rollback"))) == 1
    assert (store.receipts_root / f".{binding.identity.identity_digest}.pending.json").is_file()
    assert not (store.receipts_root / f"{binding.identity.identity_digest}.json").exists()

    monkeypatch.undo()
    store.promote(binding.identity, staged)
    assert (project / "left.txt").read_bytes() == b"left-after"
    assert (project / "right.txt").read_bytes() == b"right-after"
    assert tuple(project.glob(".*.rollback")) == ()
