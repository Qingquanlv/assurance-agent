from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

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
