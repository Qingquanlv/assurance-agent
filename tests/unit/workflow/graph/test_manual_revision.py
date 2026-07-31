"""Bounded revision views and exact manual-revision candidate capture."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from assurance_agent.workflow.graph import manual_revision as manual_revision_mod
from assurance_agent.workflow.graph.manual_revision import (
    ManualRevisionError,
    capture_revision_candidate,
    materialize_revision_view,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError


def _make_change(tmp_path: Path) -> tuple[Path, Path, TreeStore, str]:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    plans = change / "plans"
    plans.mkdir(parents=True)
    (plans / "fuzz-plan.md").write_text("# original\n", encoding="utf-8")
    (plans / "fuzz-codegen-plan.md").write_text("# codegen\n", encoding="utf-8")
    (change / "review").mkdir()
    (change / "review" / "fuzz-plan-review.json").write_text("{}\n", encoding="utf-8")
    (project / "app").mkdir(parents=True)
    (project / "app" / "source.py").write_text("app\n", encoding="utf-8")
    store = TreeStore(change)
    return project, change, store, store.capture(project)


_PATHS = ("change:plans/fuzz-codegen-plan.md", "change:plans/fuzz-plan.md")


def test_materialize_revision_view_writes_exact_allowlisted_files_only(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )

    assert binding.view_relpath == ".graph-runtime/revision-views/int-1"
    view = change / binding.view_relpath
    assert (view / "plans" / "fuzz-plan.md").read_bytes() == b"# original\n"
    assert (view / "plans" / "fuzz-codegen-plan.md").read_bytes() == b"# codegen\n"
    assert not (view / "review").exists()
    assert binding.logical_paths == _PATHS
    assert {item.logical_path: item.sha256 for item in binding.baseline} == {
        "change:plans/fuzz-codegen-plan.md": hashlib.sha256(b"# codegen\n").hexdigest(),
        "change:plans/fuzz-plan.md": hashlib.sha256(b"# original\n").hexdigest(),
    }


def test_orphan_view_is_recreated_from_base_tree(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    orphan = change / ".graph-runtime" / "revision-views" / "int-1" / "plans"
    orphan.mkdir(parents=True)
    (orphan / "fuzz-plan.md").write_text("# stale edit\n", encoding="utf-8")
    (orphan / "extra.md").write_text("extra\n", encoding="utf-8")

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )

    view = change / binding.view_relpath
    assert (view / "plans" / "fuzz-plan.md").read_bytes() == b"# original\n"
    assert not (view / "plans" / "extra.md").exists()


def test_committed_binding_preserves_user_edits(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    edited = change / binding.view_relpath / "plans" / "fuzz-plan.md"
    edited.write_text("# user edit\n", encoding="utf-8")

    rebound = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=binding,
    )

    assert rebound == binding
    assert edited.read_bytes() == b"# user edit\n"


def test_capture_rejects_missing_extra_symlink_and_non_file(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath

    (view / "plans" / "fuzz-plan.md").unlink()
    with pytest.raises((ManualRevisionError, WorkspaceError), match="missing|inventory|allowlist"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-2",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "extra.md").write_text("nope\n", encoding="utf-8")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="extra|inventory|allowlist"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-3",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "fuzz-plan.md").unlink()
    os.symlink("/tmp/outside-secret", view / "plans" / "fuzz-plan.md")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="symlink"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-4",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "fuzz-plan.md").unlink()
    os.mkfifo(view / "plans" / "fuzz-plan.md")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="non-file|regular file|inventory"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)


def test_capture_fails_closed_on_symlink_race_without_following_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-race",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    outside = tmp_path / "outside-secret"
    outside.write_text("SECRET\n", encoding="utf-8")
    target = view / "plans" / "fuzz-plan.md"
    read_outside = False

    def _race(_view_root: Path) -> None:
        nonlocal read_outside
        target.unlink()
        os.symlink(outside, target)
        # Prove capture must not open/follow the symlink target.
        original_read = Path.read_bytes

        def guarded(self: Path) -> bytes:
            nonlocal read_outside
            if self.resolve() == outside.resolve():
                read_outside = True
            return original_read(self)

        monkeypatch.setattr(Path, "read_bytes", guarded)

    monkeypatch.setattr(manual_revision_mod, "_after_revision_inventory_hook", _race)

    with pytest.raises((ManualRevisionError, WorkspaceError, OSError)):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)
    assert read_outside is False
    assert outside.read_text(encoding="utf-8") == "SECRET\n"


def test_capture_mixed_change_succeeds_and_noop_rejects(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    (change / binding.view_relpath / "plans" / "fuzz-plan.md").write_text("# revised\n", encoding="utf-8")

    revision = capture_revision_candidate(change_dir=change, store=store, binding=binding)
    assert revision.target_tree_id != base_tree_id
    assert [path.logical_path for path in revision.paths] == list(_PATHS)
    assert revision.paths[0].before_sha256 == revision.paths[0].after_sha256
    assert revision.paths[1].before_sha256 != revision.paths[1].after_sha256
    assert store.read_bytes(revision.target_tree_id, "change:plans/fuzz-plan.md") == b"# revised\n"
    assert store.read_bytes(revision.target_tree_id, "change:review/fuzz-plan-review.json") == b"{}\n"

    binding_noop = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-noop",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    with pytest.raises((ManualRevisionError, WorkspaceError), match="manual_plan_revision_noop"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding_noop)
