from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

import graph_engine.runtime.task_workspace as task_workspace
from graph_engine.runtime.task_workspace import TaskWorkspaceStore, TaskWorkspaceViolation


def _store(tmp_path: Path) -> tuple[TaskWorkspaceStore, Path, Path]:
    project = tmp_path / "project"
    attempts = tmp_path / "attempts"
    receipts = tmp_path / "receipts"
    project.mkdir()
    return TaskWorkspaceStore(project, attempts, receipts), project, attempts


def _begin(store: TaskWorkspaceStore, *, claims: tuple[str, ...] = ("out",)):
    return store.begin(task_id="task/unsafe-but-stable", attempt=1, output_paths=claims)


def test_begin_creates_empty_attempt_root_with_stable_path_free_identity(tmp_path: Path) -> None:
    store, project, attempts = _store(tmp_path)

    first = _begin(store)
    second = _begin(store)

    assert first.project_root == project.resolve()
    assert (
        first.write_root
        == attempts.resolve()
        / hashlib.sha256(b"task/unsafe-but-stable").hexdigest()
        / first.identity.attempt_id
    )
    assert list(first.write_root.iterdir()) == []
    assert first.identity == second.identity
    assert str(project) not in first.identity.model_dump_json()
    assert str(first.write_root) not in first.identity.model_dump_json()


def test_begin_accepts_an_empty_write_claim_set(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)

    binding = store.begin(task_id="read-only", attempt=1, output_paths=())

    assert binding.identity.output_paths == ()
    assert store.seal(binding.identity).files == ()


def test_begin_captures_only_declared_output_baselines(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out").mkdir()
    (project / "out" / "nested.txt").write_bytes(b"before")
    (project / "ignored.txt").write_bytes(b"ignored")

    binding = _begin(store)

    assert [
        (file.path, file.before_sha256, file.after_sha256) for file in binding.identity.baseline_files
    ] == [("out/nested.txt", hashlib.sha256(b"before").hexdigest(), None)]


def test_seal_records_only_declared_regular_staged_files(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "created.txt").write_bytes(b"new")

    staged = store.seal(binding.identity)

    assert [(file.path, file.before_sha256, file.after_sha256) for file in staged.files] == [
        ("out/created.txt", None, hashlib.sha256(b"new").hexdigest())
    ]


def test_seal_rejects_undeclared_staged_file(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    (binding.write_root / "not-claimed.txt").write_bytes(b"no")

    with pytest.raises(TaskWorkspaceViolation, match="write claim"):
        store.seal(binding.identity)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_seal_rejects_nonprivate_or_nonregular_staged_entries(tmp_path: Path, kind: str) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    target = binding.write_root / "out"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    elif kind == "hardlink":
        source = tmp_path / "source"
        source.write_bytes(b"same")
        os.link(source, target)
    else:
        os.mkfifo(target)

    with pytest.raises(TaskWorkspaceViolation):
        store.seal(binding.identity)


def test_promote_rejects_target_drift_since_begin(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out.txt").write_bytes(b"before")
    binding = _begin(store, claims=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    (project / "out.txt").write_bytes(b"drifted")

    with pytest.raises(TaskWorkspaceViolation, match="target drift"):
        store.promote(binding.identity, staged)


def test_promote_replaces_enumerated_files_and_writes_a_durable_receipt(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out").mkdir()
    (project / "out" / "old.txt").write_bytes(b"old")
    (project / "out" / "unlisted.txt").write_bytes(b"keep")
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "old.txt").write_bytes(b"new")
    (binding.write_root / "out" / "new.txt").write_bytes(b"new file")

    receipt = store.promote(binding.identity, store.seal(binding.identity))

    assert (project / "out" / "old.txt").read_bytes() == b"new"
    assert (project / "out" / "new.txt").read_bytes() == b"new file"
    assert (project / "out" / "unlisted.txt").read_bytes() == b"keep"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()


def test_identical_promotion_replay_is_idempotent_but_different_bytes_fail_closed(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    binding = _begin(store, claims=("out.txt",))
    target = binding.write_root / "out.txt"
    target.write_bytes(b"first")
    staged = store.seal(binding.identity)
    receipt = store.promote(binding.identity, staged)

    assert store.promote(binding.identity, staged) == receipt
    target.write_bytes(b"different")
    different = store.seal(binding.identity)
    with pytest.raises(TaskWorkspaceViolation, match="receipt"):
        store.promote(binding.identity, different)
    assert (project / "out.txt").read_bytes() == b"first"


@pytest.mark.parametrize("mutation", ["staged", "target", "deleted-target"])
def test_completed_receipt_replay_reauthenticates_staged_and_target_state(
    tmp_path: Path, mutation: str
) -> None:
    store, project, _attempts = _store(tmp_path)
    binding = _begin(store, claims=("out.txt",))
    staged_file = binding.write_root / "out.txt"
    staged_file.write_bytes(b"first")
    staged = store.seal(binding.identity)
    store.promote(binding.identity, staged)
    if mutation == "staged":
        staged_file.write_bytes(b"changed")
    elif mutation == "target":
        (project / "out.txt").write_bytes(b"changed")
    else:
        (project / "out.txt").unlink()

    with pytest.raises(TaskWorkspaceViolation):
        store.promote(binding.identity, staged)


def test_ancestor_symlink_swap_cannot_redirect_descriptor_bound_target_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, _attempts = _store(tmp_path)
    target_parent = project / "out"
    target_parent.mkdir()
    (target_parent / "target.txt").write_bytes(b"before")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "target.txt"
    sentinel.write_bytes(b"outside")
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "target.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)

    real_replace = os.replace

    def swap_parent_then_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == "target.txt" and "dst_dir_fd" in kwargs:
            project_parent = project / "out"
            if project_parent.is_dir() and not project_parent.is_symlink():
                shutil.move(str(project_parent), str(project / "out-real"))
                project_parent.symlink_to(outside, target_is_directory=True)
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", swap_parent_then_replace)

    with pytest.raises(TaskWorkspaceViolation):
        store.promote(binding.identity, staged)

    assert sentinel.read_bytes() == b"outside"
    assert (project / "out-real" / "target.txt").read_bytes() == b"after"


def test_ancestor_symlink_swap_cannot_redirect_baseline_or_staged_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out").mkdir()
    (project / "out" / "baseline.txt").write_bytes(b"baseline")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    real_open = os.open

    def swap_baseline_parent(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "out" and kwargs.get("dir_fd") is not None:
            current = project / "out"
            if current.is_dir() and not current.is_symlink():
                shutil.move(str(current), str(project / "out-real"))
                current.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "open", swap_baseline_parent)

    with pytest.raises(TaskWorkspaceViolation):
        store.begin(task_id="baseline", attempt=1, output_paths=("out",))
    assert sentinel.read_bytes() == b"outside"

    monkeypatch.undo()
    binding = store.begin(task_id="staged", attempt=1, output_paths=("staged",))
    (binding.write_root / "staged").mkdir()
    (binding.write_root / "staged" / "file.txt").write_bytes(b"staged")
    staging_outside = tmp_path / "staging-outside"
    staging_outside.mkdir()
    staging_sentinel = staging_outside / "sentinel.txt"
    staging_sentinel.write_bytes(b"outside")

    def swap_staged_parent(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "staged" and kwargs.get("dir_fd") is not None:
            current = binding.write_root / "staged"
            if current.is_dir() and not current.is_symlink():
                shutil.move(str(current), str(binding.write_root / "staged-real"))
                current.symlink_to(staging_outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "open", swap_staged_parent)
    with pytest.raises(TaskWorkspaceViolation):
        store.seal(binding.identity)
    assert staging_sentinel.read_bytes() == b"outside"
