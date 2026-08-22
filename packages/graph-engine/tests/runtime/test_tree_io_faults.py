from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from graph_engine.runtime.seed import SeedFile
from graph_engine.runtime.tree_io import (
    SeedCaptureError,
    SeedCapturePolicy,
    SnapshotExportError,
    capture_workspace_seed,
    materialize_snapshot,
)
from graph_engine.runtime.workspace import SnapshotStore, WorkspaceViolation, _OpenedTree


def test_capture_rejects_file_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * 32)
    policy = SeedCapturePolicy(maximum_file_bytes=16)
    with pytest.raises(SeedCaptureError, match="maximum_file_bytes"):
        capture_workspace_seed(source, policy=policy)


def test_capture_rejects_total_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a" * 20)
    (source / "b.bin").write_bytes(b"b" * 20)
    policy = SeedCapturePolicy(maximum_total_bytes=30)
    with pytest.raises(SeedCaptureError, match="maximum_total_bytes"):
        capture_workspace_seed(source, policy=policy)


def test_capture_rejects_file_count_limit(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    for index in range(3):
        (source / f"{index}.txt").write_text(str(index), encoding="utf-8")
    policy = SeedCapturePolicy(maximum_file_count=2)
    with pytest.raises(SeedCaptureError, match="maximum_file_count"):
        capture_workspace_seed(source, policy=policy)


def test_capture_rejects_special_file(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    source = tmp_path / "sut"
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(SeedCaptureError, match="regular file"):
        capture_workspace_seed(source, policy=SeedCapturePolicy())


def test_capture_rejects_path_drift_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "drift.txt").write_text("stable", encoding="utf-8")

    def fail_stable(*_args: object, **_kwargs: object) -> None:
        raise WorkspaceViolation("captured file changed during read: drift.txt")

    monkeypatch.setattr("graph_engine.runtime.tree_io._assert_open_file_stable", fail_stable)
    with pytest.raises(SeedCaptureError, match="changed"):
        capture_workspace_seed(source, policy=SeedCapturePolicy())


def test_capture_verifies_digest_before_close(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    content = b"digest-check"
    (source / "checked.txt").write_bytes(content)
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    file = seed.files[0]
    assert file.sha256 == hashlib.sha256(content).hexdigest()
    assert file.content == content


def test_capture_rejects_duplicate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    duplicate_a = SeedFile(path="same.txt", sha256=hashlib.sha256(b"a").hexdigest(), content=b"a")
    duplicate_b = SeedFile(path="same.txt", sha256=hashlib.sha256(b"b").hexdigest(), content=b"b")

    def fake_capture(_source: Path, _policy: SeedCapturePolicy) -> tuple[SeedFile, ...]:
        return (duplicate_a, duplicate_b)

    monkeypatch.setattr(
        "graph_engine.runtime.tree_io._capture_regular_files_beneath",
        fake_capture,
    )
    with pytest.raises(SeedCaptureError, match="duplicate"):
        capture_workspace_seed(tmp_path / "sut", policy=SeedCapturePolicy())


def test_materialize_rejects_non_canonical_snapshot_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"ok.txt": b"ok"})
    tree_id = store.head_tree_id()
    real_open_tree = SnapshotStore._open_tree

    def fake_open_tree(self: SnapshotStore, trees_fd: int, requested_tree_id: str) -> _OpenedTree:
        tree = real_open_tree(self, trees_fd, requested_tree_id)
        os.close(tree.descriptor)
        return _OpenedTree(
            descriptor=real_open_tree(self, trees_fd, requested_tree_id).descriptor,
            identity=tree.identity,
            manifest={"../escape": hashlib.sha256(b"x").hexdigest(), "ok.txt": hashlib.sha256(b"ok").hexdigest()},
        )

    monkeypatch.setattr(SnapshotStore, "_open_tree", fake_open_tree)
    destination = tmp_path / "export"
    with pytest.raises(SnapshotExportError, match="relative canonical path"):
        materialize_snapshot(store, tree_id, destination)


def test_materialize_interrupted_export_leaves_no_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"payload"})
    tree_id = store.head_tree_id()
    destination = tmp_path / "export"

    def fail_rename(src: str, dst: str, *args: object, **kwargs: object) -> None:
        del src, dst, args, kwargs
        raise OSError("simulated export interruption")

    monkeypatch.setattr(os, "rename", fail_rename)
    with pytest.raises(SnapshotExportError):
        materialize_snapshot(store, tree_id, destination)
    assert not destination.exists()
    assert not any(path.name.startswith(".export-staging-") for path in tmp_path.iterdir())


def test_materialize_fails_for_missing_tree(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"payload"})
    destination = tmp_path / "export"
    missing_tree_id = "0" * 64
    with pytest.raises((SnapshotExportError, WorkspaceViolation)):
        materialize_snapshot(store, missing_tree_id, destination)
