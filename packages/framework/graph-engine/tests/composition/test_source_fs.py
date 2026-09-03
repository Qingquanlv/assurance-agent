from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import graph_engine.composition.source_fs as source_fs
from graph_engine.composition import (
    DeclaredTreePolicy,
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
    SourceSnapshotError,
    capture_declared_tree,
    capture_explicit_file,
    recapture_declared_files,
)


def test_declared_tree_digest_is_path_order_independent(tmp_path: Path) -> None:
    plugin_bytes = b"plugin_id: toy.flow\n"
    workflow_bytes = b"name: flow\n"
    (tmp_path / "plugin.yaml").write_bytes(plugin_bytes)
    (tmp_path / "workflow.yaml").write_bytes(workflow_bytes)

    first = capture_declared_tree(
        tmp_path,
        ("workflow.yaml", "plugin.yaml"),
        DeclaredTreePolicy.config_tree(),
    )
    second = capture_declared_tree(
        tmp_path,
        ("plugin.yaml", "workflow.yaml"),
        DeclaredTreePolicy.config_tree(),
    )

    assert first == second
    assert first.identity.kind == SourceKind.CONFIG_TREE
    assert first.identity.root == tmp_path.resolve()
    assert tuple(item.path for item in first.files) == ("plugin.yaml", "workflow.yaml")
    assert tuple(item.content for item in first.files) == (plugin_bytes, workflow_bytes)
    assert first.digest == "d88a72feb61e2cebf5694a3cfd8087483f306b73e1bd6ce4553f69532363ebe3"


def test_declared_tree_returns_frozen_bytes_and_models(tmp_path: Path) -> None:
    (tmp_path / "plugin.yaml").write_bytes(b"plugin_id: toy.flow\n")

    snapshot = capture_declared_tree(
        tmp_path,
        ("plugin.yaml",),
        DeclaredTreePolicy.config_tree(),
    )

    assert isinstance(snapshot.files, tuple)
    assert isinstance(snapshot.files[0].content, bytes)
    with pytest.raises(AttributeError):
        snapshot.digest = "0" * 64  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.files[0].content = b"changed"  # type: ignore[misc]


def test_explicit_file_capture_rejects_unrescanned_directory_chains(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "product.yaml").write_text("product_id: toy.product\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="one path segment"):
        capture_explicit_file(
            tmp_path,
            "nested/product.yaml",
            DeclaredTreePolicy.product_file(),
        )


def test_source_models_reject_mutable_file_lists_and_unsafe_paths(tmp_path: Path) -> None:
    source_file = SourceFile.from_bytes("plugin.yaml", b"plugin_id: toy.flow\n")
    valid = SourceSnapshot.from_files(SourceKind.CONFIG_TREE, tmp_path.resolve(), (source_file,))

    with pytest.raises(TypeError, match="files must be an immutable tuple"):
        SourceSnapshot(
            identity=SourceIdentity(SourceKind.CONFIG_TREE, tmp_path.resolve()),
            files=[source_file],  # type: ignore[arg-type]
            digest=valid.digest,
        )
    with pytest.raises(ValueError, match="canonical relative path"):
        SourceFile.from_bytes("../escape", b"outside\n")
    with pytest.raises(TypeError, match="policy kind must be a SourceKind"):
        DeclaredTreePolicy(kind="config_tree")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_path",
    (
        "",
        ".",
        "./a",
        "../escape",
        "/absolute",
        "a/../b",
        "a//b",
        "a\\b",
        "nul\0byte",
    ),
)
def test_declared_tree_rejects_unsafe_paths(tmp_path: Path, bad_path: str) -> None:
    with pytest.raises(SourceSnapshotError, match="unsafe declared source path"):
        capture_declared_tree(tmp_path, (bad_path,), DeclaredTreePolicy.config_tree())


def test_declared_tree_rejects_duplicate_paths(tmp_path: Path) -> None:
    (tmp_path / "plugin.yaml").write_text("plugin_id: toy.flow\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="duplicate declared source path"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml", "plugin.yaml"),
            DeclaredTreePolicy.config_tree(),
        )


def test_recapture_declared_files_ignores_sibling_site_packages(tmp_path: Path) -> None:
    package = tmp_path / "graph_engine_toy_a"
    package.mkdir()
    declared = "graph_engine_toy_a/product.py"
    (package / "product.py").write_bytes(b"def build() -> None:\n    return None\n")
    sibling = tmp_path / "pydantic"
    sibling.mkdir()
    (sibling / "__init__.py").write_bytes(b"")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "product.cpython-311.pyc").write_bytes(b"\0")

    with pytest.raises(
        SourceSnapshotError,
        match="declared source file set does not match the physical tree",
    ):
        capture_declared_tree(
            tmp_path,
            (declared,),
            DeclaredTreePolicy(kind=SourceKind.WHEEL_PRODUCT),
        )

    captured = recapture_declared_files(
        tmp_path,
        (declared,),
        DeclaredTreePolicy(kind=SourceKind.WHEEL_PRODUCT),
    )
    assert tuple(item.path for item in captured) == (declared,)
    assert captured[0].content == b"def build() -> None:\n    return None\n"


@pytest.mark.parametrize("declared", ((), ("plugin.yaml", "missing.yaml")))
def test_declared_tree_requires_exact_physical_file_set(
    tmp_path: Path,
    declared: tuple[str, ...],
) -> None:
    (tmp_path / "plugin.yaml").write_text("plugin_id: toy.flow\n", encoding="utf-8")

    with pytest.raises(
        SourceSnapshotError,
        match="declared source file set does not match the physical tree",
    ):
        capture_declared_tree(tmp_path, declared, DeclaredTreePolicy.config_tree())


@pytest.mark.parametrize("symlink_kind", ("file", "directory"))
def test_declared_tree_rejects_symlinked_entries(tmp_path: Path, symlink_kind: str) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    if symlink_kind == "file":
        outside.write_text("outside\n", encoding="utf-8")
        (tmp_path / "plugin.yaml").symlink_to(outside)
        declared = ("plugin.yaml",)
    else:
        outside.mkdir()
        (outside / "plugin.yaml").write_text("outside\n", encoding="utf-8")
        (tmp_path / "nested").symlink_to(outside, target_is_directory=True)
        declared = ("nested/plugin.yaml",)

    with pytest.raises(SourceSnapshotError, match="no-follow|regular file"):
        capture_declared_tree(tmp_path, declared, DeclaredTreePolicy.config_tree())


def test_declared_tree_rejects_symlinked_root(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    (physical / "plugin.yaml").write_text("plugin_id: toy.flow\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)

    with pytest.raises(SourceSnapshotError, match="no-follow directory"):
        capture_declared_tree(
            alias,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


@pytest.mark.parametrize(
    "policy",
    (DeclaredTreePolicy.config_tree(), DeclaredTreePolicy.editable()),
)
def test_editable_and_config_trees_reject_hard_links(
    tmp_path: Path,
    policy: DeclaredTreePolicy,
) -> None:
    source = tmp_path / "plugin.yaml"
    source.write_text("plugin_id: toy.flow\n", encoding="utf-8")
    os.link(source, tmp_path / "alias.yaml")

    with pytest.raises(SourceSnapshotError, match="hard link is not allowed"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml", "alias.yaml"),
            policy,
        )


def test_config_tree_rejects_executable_regular_files(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.yaml"
    plugin.write_text("plugin_id: toy.flow\n", encoding="utf-8")
    plugin.chmod(0o700)

    with pytest.raises(SourceSnapshotError, match="executable file is not allowed"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


@pytest.mark.parametrize("replacement", ("regular", "symlink"))
def test_declared_tree_rejects_path_swap_during_component_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    path = tmp_path / "plugin.yaml"
    path.write_text("original\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-replacement"
    outside.write_text("replacement\n", encoding="utf-8")
    swapped = False

    def swap(phase: str, relative_path: str | None) -> None:
        nonlocal swapped
        if phase != "before_component_open" or relative_path != "plugin.yaml" or swapped:
            return
        swapped = True
        path.unlink()
        if replacement == "regular":
            path.write_text("replacement\n", encoding="utf-8")
        else:
            path.symlink_to(outside)

    monkeypatch.setattr(source_fs, "_snapshot_boundary", swap)

    with pytest.raises(SourceSnapshotError, match="changed while opening|safely open"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_rejects_regular_to_fifo_swap_without_blocking(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        import signal
        import sys
        from pathlib import Path

        import graph_engine.composition.source_fs as source_fs
        from graph_engine.composition import (
            DeclaredTreePolicy,
            SourceSnapshotError,
            capture_declared_tree,
        )

        root = Path(sys.argv[1])
        path = root / "plugin.yaml"
        path.write_text("original\\n", encoding="utf-8")
        swapped = False

        def swap(phase: str, relative_path: str | None) -> None:
            global swapped
            if phase != "before_component_open" or relative_path != "plugin.yaml" or swapped:
                return
            swapped = True
            path.unlink()
            os.mkfifo(path)

        source_fs._snapshot_boundary = swap
        signal.alarm(1)
        try:
            capture_declared_tree(
                root,
                ("plugin.yaml",),
                DeclaredTreePolicy.config_tree(),
            )
        except SourceSnapshotError:
            signal.alarm(0)
            raise SystemExit(0)
        raise SystemExit(2)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_declared_tree_rejects_byte_mutation_after_first_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin.yaml"
    path.write_text("original\n", encoding="utf-8")

    def mutate(phase: str, relative_path: str | None) -> None:
        if phase == "before_read" and relative_path == "plugin.yaml":
            path.write_text("mutated-and-longer\n", encoding="utf-8")

    monkeypatch.setattr(source_fs, "_snapshot_boundary", mutate)

    with pytest.raises(SourceSnapshotError, match="changed while it was read"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


@pytest.mark.parametrize("mutation", ("add", "remove"))
def test_declared_tree_rejects_name_set_change_during_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    plugin = tmp_path / "plugin.yaml"
    plugin.write_text("plugin_id: toy.flow\n", encoding="utf-8")

    def mutate(phase: str, relative_path: str | None) -> None:
        if phase != "before_rescan":
            return
        if mutation == "add":
            (tmp_path / "added.yaml").write_text("added\n", encoding="utf-8")
        else:
            plugin.unlink()

    monkeypatch.setattr(source_fs, "_snapshot_boundary", mutate)

    with pytest.raises(SourceSnapshotError, match="source tree changed while it was captured"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_rejects_replacement_before_final_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin.yaml"
    path.write_text("original\n", encoding="utf-8")

    def replace(phase: str, relative_path: str | None) -> None:
        if phase == "before_final_stat" and relative_path == "plugin.yaml":
            path.unlink()
            path.write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(source_fs, "_snapshot_boundary", replace)

    with pytest.raises(SourceSnapshotError, match="changed while it was read"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_rejects_mutation_after_final_stat_during_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plugin.yaml"
    path.write_text("original\n", encoding="utf-8")
    mutated = False

    def mutate(phase: str, relative_path: str | None) -> None:
        nonlocal mutated
        if phase == "after_final_stat" and relative_path == "plugin.yaml" and not mutated:
            mutated = True
            path.write_text("replacement-after-final-stat\n", encoding="utf-8")

    monkeypatch.setattr(source_fs, "_snapshot_boundary", mutate)

    with pytest.raises(SourceSnapshotError, match="source tree changed while it was captured"):
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_rejects_root_replacement_after_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "plugin.yaml").write_text("original\n", encoding="utf-8")
    replaced = False

    def replace_root(phase: str, relative_path: str | None) -> None:
        nonlocal replaced
        if phase != "after_rescan" or replaced:
            return
        replaced = True
        root.rename(tmp_path / "original-root")
        root.mkdir()
        (root / "plugin.yaml").write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(source_fs, "_snapshot_boundary", replace_root)

    with pytest.raises(SourceSnapshotError, match="source root changed while it was captured"):
        capture_declared_tree(
            root,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_authenticates_the_resolved_root_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "plugin.yaml").write_text("original\n", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    real_resolve = Path.resolve

    def resolve_to_unrelated(path: Path, *args: object, **kwargs: object) -> Path:
        if path == root:
            return unrelated
        return real_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", resolve_to_unrelated)

    with pytest.raises(SourceSnapshotError, match="source root changed while it was captured"):
        capture_declared_tree(
            root,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )


def test_declared_tree_retains_primary_error_when_descriptor_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plugin.yaml").write_text("plugin_id: toy.flow\n", encoding="utf-8")

    class PrimaryError(RuntimeError):
        pass

    class CleanupError(RuntimeError):
        pass

    primary = PrimaryError("read failed")
    real_close = os.close
    primary_raised = False

    def fail_read(phase: str, relative_path: str | None) -> None:
        nonlocal primary_raised
        if phase == "before_read" and relative_path == "plugin.yaml":
            primary_raised = True
            raise primary

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        if primary_raised:
            raise CleanupError("close failed")

    monkeypatch.setattr(source_fs, "_snapshot_boundary", fail_read)
    monkeypatch.setattr(source_fs, "_close_descriptor", close_then_fail)

    with pytest.raises(PrimaryError) as captured:
        capture_declared_tree(
            tmp_path,
            ("plugin.yaml",),
            DeclaredTreePolicy.config_tree(),
        )
    assert captured.value is primary
    assert captured.value.__cause__ is None


def test_declared_tree_surfaces_cleanup_error_without_a_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = os.close

    class CleanupError(RuntimeError):
        pass

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise CleanupError("close failed")

    monkeypatch.setattr(source_fs, "_close_descriptor", close_then_fail)

    with pytest.raises(CleanupError, match="close failed"):
        capture_declared_tree(tmp_path, (), DeclaredTreePolicy.config_tree())


def test_snapshot_file_sha256_is_of_the_captured_bytes(tmp_path: Path) -> None:
    content = b"plugin_id: toy.flow\n"
    (tmp_path / "plugin.yaml").write_bytes(content)

    snapshot = capture_declared_tree(
        tmp_path,
        ("plugin.yaml",),
        DeclaredTreePolicy.config_tree(),
    )

    assert snapshot.files[0].sha256 == hashlib.sha256(content).hexdigest()
