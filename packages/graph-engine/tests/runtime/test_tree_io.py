from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graph_engine.runtime.seed import SeedFile, WorkspaceSeed, workspace_tree_id
from graph_engine.runtime.tree_io import (
    ExportManifest,
    ExportedFile,
    SeedCaptureError,
    SeedCapturePolicy,
    SnapshotExportError,
    capture_workspace_seed,
    materialize_snapshot,
)
from graph_engine.runtime.workspace import SnapshotStore


def test_capture_is_stable_after_source_mutation(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "app.py").write_text("before", encoding="utf-8")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    (source / "app.py").write_text("after", encoding="utf-8")
    assert seed.files[0].content == b"before"


def test_capture_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "escape").symlink_to(tmp_path)
    with pytest.raises(SeedCaptureError, match="symlink"):
        capture_workspace_seed(source, policy=SeedCapturePolicy())


def test_capture_excludes_policy_names(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "app.py").write_text("visible", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-311.pyc").write_bytes(b"hidden")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    assert [item.path for item in seed.files] == ["app.py"]


def test_capture_deterministic_order(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "b.txt").write_text("b", encoding="utf-8")
    (source / "a.txt").write_text("a", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "c.txt").write_text("c", encoding="utf-8")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    assert [item.path for item in seed.files] == ["a.txt", "b.txt", "nested/c.txt"]


def test_capture_empty_workspace(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    assert seed.files == ()
    assert seed.tree_id == workspace_tree_id({})


def test_materialize_snapshot_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "app.py").write_text("hello", encoding="utf-8")
    nested = source / "pkg"
    nested.mkdir()
    (nested / "mod.py").write_text("world", encoding="utf-8")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())

    initial = {item.path: item.content for item in seed.files}
    store = SnapshotStore.create(tmp_path / "store", initial)
    destination = tmp_path / "export"
    manifest = materialize_snapshot(store, seed.tree_id, destination)

    assert destination.is_dir()
    assert (destination / "app.py").read_bytes() == b"hello"
    assert (destination / "pkg" / "mod.py").read_bytes() == b"world"
    assert manifest.tree_id == seed.tree_id
    assert tuple(item.path for item in manifest.files) == tuple(item.path for item in seed.files)


def test_materialize_snapshot_rejects_existing_destination(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"seed"})
    destination = tmp_path / "export"
    destination.mkdir()
    with pytest.raises(SnapshotExportError, match="destination already exists"):
        materialize_snapshot(store, store.head_tree_id(), destination)


def test_export_manifest_matches_exported_files(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"a.txt": b"alpha", "nested/b.txt": b"beta"})
    destination = tmp_path / "export"
    manifest = materialize_snapshot(store, store.head_tree_id(), destination)
    assert isinstance(manifest, ExportManifest)
    for exported in manifest.files:
        assert isinstance(exported, ExportedFile)
        content = (destination / exported.path).read_bytes()
        assert exported.sha256 == hashlib.sha256(content).hexdigest()
        assert exported.size_bytes == len(content)


def test_capture_builds_valid_workspace_seed(tmp_path: Path) -> None:
    source = tmp_path / "sut"
    source.mkdir()
    (source / "note.txt").write_text("stable", encoding="utf-8")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    assert isinstance(seed, WorkspaceSeed)
    digest_map = {item.path: item.sha256 for item in seed.files}
    assert seed.tree_id == workspace_tree_id(digest_map)
    assert all(isinstance(item, SeedFile) for item in seed.files)
