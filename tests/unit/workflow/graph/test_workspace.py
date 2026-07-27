"""task 私有 workspace 与内容寻址 write-set：安全性与确定性合并。

覆盖：确定性 tree capture 与排除规则、symlink escape、authorization_writes
之外的实际写入 fail closed、缺失 output、write-set 对象篡改、sibling 重叠
写、canonical base 漂移、partial apply 后幂等收敛、对象库独立重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph import workspace as workspace_mod
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)

_GIT = shutil.which("git")


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "input.txt").write_text("case input\n", encoding="utf-8")
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "test_a.py").write_text("base\n", encoding="utf-8")
    (project / "app").mkdir()
    (project / "app" / "source.py").write_text("app base\n", encoding="utf-8")
    return project


def _store(project: Path) -> TreeStore:
    return TreeStore(project / "qa" / "changes" / "CH-1")


def _backend(project: Path) -> WorkspaceBackend:
    return WorkspaceBackend(project / "qa" / "changes" / "CH-1")


def _claims(*patterns: str) -> ResourceClaims:
    parsed = tuple(ResourcePath.parse(p) for p in patterns)
    return ResourceClaims(writes=parsed, authorization_writes=parsed)


# ---------------------------------------------------------------------------
# capture / materialize


def test_capture_is_deterministic_and_excludes_runtime_dirs(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    for junk in (
        ".git/config",
        ".graph-runtime/objects/sha256/ab/cd",
        ".worktrees/wt/file.txt",
        ".venv/lib/python.py",
        ".opencode/skills/aws-run",
        "eval/out/runs/x/report.json",
        "benchmark/runs/x.status.json",
        "node_modules/pkg/index.js",
        "__pycache__/mod.cpython-311.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".ruff_cache/cache.json",
        ".hypothesis/unicode_data/codec.json",
        "htmlcov/index.html",
        "migrations/models/0_20260101000000_init.py",
        ".coverage",
        "db.sqlite3",
        "db.sqlite3-wal",
    ):
        path = project / junk
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk\n", encoding="utf-8")

    store = _store(project)
    first = store.capture(project)
    second = store.capture(project)
    assert first == second

    dest = tmp_path / "out"
    dest.mkdir()
    store.materialize(first, dest)
    assert (dest / "tests" / "api" / "test_a.py").read_text() == "base\n"
    assert (dest / "app" / "source.py").read_text() == "app base\n"
    assert (dest / "qa" / "changes" / "CH-1" / "input.txt").read_text() == "case input\n"
    for excluded in (
        ".git",
        ".worktrees",
        ".venv",
        ".opencode",
        "eval",
        "benchmark",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "htmlcov",
        "migrations",
    ):
        assert not (dest / excluded).exists()
    assert not (dest / ".coverage").exists()
    assert not (dest / "db.sqlite3").exists()
    assert not (dest / "db.sqlite3-wal").exists()
    # workspace 内的 .graph-runtime/tree.json 是物化元数据，不参与 diff。


def test_capture_keeps_change_issue_ledger_but_excludes_change_coordinator_ledger(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    (change / "events.jsonl").write_text('{"type":"coordinator"}\n', encoding="utf-8")
    issue_ledger = change / "issues" / "events.jsonl"
    issue_ledger.parent.mkdir(parents=True)
    issue_ledger.write_text('{"type":"observation_recorded"}\n', encoding="utf-8")
    project_issue_ledger = project / "qa" / "issues" / "events.jsonl"
    project_issue_ledger.parent.mkdir(parents=True)
    project_issue_ledger.write_text('{"type":"problem_detected"}\n', encoding="utf-8")

    store = _store(project)
    tree = store.capture(project)
    dest = tmp_path / "captured"
    dest.mkdir()
    store.materialize(tree, dest)

    assert not (dest / "qa" / "changes" / "CH-1" / "events.jsonl").exists()
    assert (dest / "qa" / "changes" / "CH-1" / "issues" / "events.jsonl").read_text(
        encoding="utf-8"
    ) == '{"type":"observation_recorded"}\n'
    assert not (dest / "qa" / "issues" / "events.jsonl").exists()
    assert (dest / ".graph-runtime" / "tree.json").exists()
    assert not (dest / ".graph-runtime" / "objects").exists()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("qa/changes/CH-1/issues/events.jsonl", True),
        ("qa/archive/CH-OLD/issues/events.jsonl", True),
        ("nested/qa/changes/CH-1/issues/events.jsonl", False),
        ("qa/changes/extra/CH-1/issues/events.jsonl", False),
        ("eval/out/runs/R/samples/S/sut/qa/changes/CH-1/issues/events.jsonl", False),
    ],
)
def test_top_level_issue_ledger_predicate_is_exact(path: str, expected: bool) -> None:
    assert workspace_mod._is_top_level_issue_ledger(path) is expected


def test_capture_keeps_archived_issue_ledger(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    ledger = project / "qa/archive/CH-OLD/issues/events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"type":"observation_recorded"}\n', encoding="utf-8")
    store = _store(project)
    tree = store.capture(project)
    dest = tmp_path / "captured-archive"
    dest.mkdir()

    store.materialize(tree, dest)

    assert (dest / "qa/archive/CH-OLD/issues/events.jsonl").read_bytes() == ledger.read_bytes()


def test_tree_store_read_json_by_logical_path(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    healing = change / "healing"
    healing.mkdir(parents=True)
    doc = {"proposals": [{"target": "api", "eligible": True}]}
    (healing / "fix-proposal.json").write_text(json.dumps(doc), encoding="utf-8")

    store = _store(project)
    tree_id = store.capture(project)
    resolved = store.read_json(tree_id, "change:healing/fix-proposal.json")
    assert resolved.value == doc
    assert resolved.reads_sha256["change:healing/fix-proposal.json"]

    with pytest.raises(FileNotFoundError):
        store.read_json(tree_id, "change:healing/missing.json")


def test_capture_rejects_symlink_escaping_root(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink(outside, project / "app" / "evil_link")
    with pytest.raises(WorkspaceError):
        _store(project).capture(project)


def test_symlinked_host_runtime_dirs_are_excluded_not_rejected(tmp_path: Path) -> None:
    """``link_host_task_paths`` reattaches ``.venv``/``node_modules`` as symlinks to the
    host copy; being excluded roots they must be skipped, not treated as escapes."""
    project = _make_project(tmp_path)
    host_venv = tmp_path / "host" / ".venv"
    host_venv.mkdir(parents=True)
    (host_venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    store = _store(project)
    base_tree = store.capture(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=base_tree, store=store)
    os.symlink(host_venv, workspace.project_root / ".venv")
    os.symlink(host_venv, workspace.project_root / "node_modules")
    (workspace.project_root / "tests" / "api" / "test_a.py").write_text("edited\n", encoding="utf-8")

    write_set = store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))
    assert [entry.logical_path for entry in write_set.entries] == ["project:tests/api/test_a.py"]

    os.symlink(host_venv, project / ".venv")
    assert store.capture(project) == base_tree


def test_capture_preserves_executable_mode(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    script = project / "app" / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)

    store = _store(project)
    tree = store.capture(project)
    dest = tmp_path / "out"
    dest.mkdir()
    store.materialize(tree, dest)
    assert os.access(dest / "app" / "run.sh", os.X_OK)
    assert not os.access(dest / "app" / "source.py", os.X_OK)


# ---------------------------------------------------------------------------
# freeze_write_set


def test_freeze_write_set_roundtrip_and_canonical_untouched(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    workspace = backend.create(task_id="task-a", base_tree_id=base_tree, store=store)
    (workspace.project_root / "tests/api/test_a.py").write_text("changed\n")
    write_set = store.freeze_write_set(
        workspace,
        claims=ResourceClaims(
            writes=(ResourcePath.parse("repo:tests/api/**"),),
            authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
        ),
        outputs=("repo:tests/api/test_a.py",),
    )
    assert store.load_write_set(write_set.write_set_id) == write_set
    assert (project / "tests/api/test_a.py").read_text() == "base\n"
    assert set(write_set.outputs_sha256) == {"repo:tests/api/test_a.py"}
    assert len(write_set.entries) == 1
    entry = write_set.entries[0]
    assert entry.operation == "modify"
    assert entry.before_sha256 is not None and entry.after_sha256 is not None
    assert entry.blob_sha256 == entry.after_sha256


def test_freeze_empty_diff_produces_empty_entries(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    write_set = store.freeze_write_set(workspace, claims=_claims("repo:**"))
    assert write_set.entries == ()
    assert write_set.outputs_sha256 == {}


def test_freeze_rejects_write_outside_authorization(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    (workspace.project_root / "app" / "source.py").write_text("changed\n")
    with pytest.raises(WorkspaceError, match="forbidden"):
        store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))


def test_freeze_rejects_missing_declared_output(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    (workspace.project_root / "tests" / "api" / "test_a.py").write_text("changed\n")
    with pytest.raises(WorkspaceError, match="output"):
        store.freeze_write_set(
            workspace,
            claims=_claims("repo:tests/api/**"),
            outputs=("repo:tests/api/missing.py",),
        )


def test_freeze_accepts_directory_output_with_files(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    case = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case.parent.mkdir(parents=True)
    case.write_text("id: c1\n", encoding="utf-8")
    write_set = store.freeze_write_set(
        workspace,
        claims=_claims("change:cases/**"),
        outputs=("change:cases/",),
    )
    assert "change:cases/" in write_set.outputs_sha256
    assert any(e.logical_path.endswith("cases/system/dept/case.yaml") for e in write_set.entries)


def test_freeze_rejects_empty_directory_output(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    (workspace.change_dir / "cases").mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="directory empty"):
        store.freeze_write_set(
            workspace,
            claims=_claims("change:cases/**"),
            outputs=("change:cases/",),
        )


def test_capture_skips_sibling_change_dirs(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    sibling = project / "qa" / "changes" / "OTHER"
    sibling.mkdir(parents=True)
    (sibling / "leak.txt").write_text("nope\n", encoding="utf-8")
    (project / "qa" / "changes" / "CH-1" / "driver.json").write_text("{}\n", encoding="utf-8")
    (project / "qa" / "changes" / "CH-1" / "driver.lock").write_text("1\ntoken\n", encoding="utf-8")
    store = _store(project)
    tree = store.capture(project)
    dest = tmp_path / "out"
    dest.mkdir()
    store.materialize(tree, dest)
    assert not (dest / "qa" / "changes" / "OTHER").exists()
    assert not (dest / "qa" / "changes" / "CH-1" / "driver.json").exists()
    assert not (dest / "qa" / "changes" / "CH-1" / "driver.lock").exists()


def test_freeze_rejects_symlink_escape(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    os.symlink(outside, workspace.project_root / "tests" / "api" / "evil_link")
    with pytest.raises(WorkspaceError):
        store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))


def test_freeze_rejects_added_symlink_even_inside_root(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    os.symlink("test_a.py", workspace.project_root / "tests" / "api" / "link.py")
    with pytest.raises(WorkspaceError, match="symlink"):
        store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))


def test_freeze_rejects_glob_output(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    with pytest.raises(WorkspaceError, match="output"):
        store.freeze_write_set(
            workspace,
            claims=_claims("repo:tests/api/**"),
            outputs=("repo:tests/api/*",),
        )


def test_load_write_set_rejects_tampered_object_bytes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    (workspace.project_root / "tests" / "api" / "test_a.py").write_text("changed\n")
    write_set = store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))

    objects = project / "qa" / "changes" / "CH-1" / ".graph-runtime" / "objects"
    target = next(objects.rglob(write_set.write_set_id))
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0xFF
    target.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceError):
        store.load_write_set(write_set.write_set_id)


def test_workspace_rematerializes_from_object_store_after_cleanup(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    workspace = backend.create(task_id="task-a", base_tree_id=base_tree, store=store)
    (workspace.project_root / "tests" / "api" / "test_a.py").write_text("changed\n")
    write_set = store.freeze_write_set(workspace, claims=_claims("repo:tests/api/**"))

    workspace.cleanup()
    assert not workspace.root.exists()

    recreated = backend.create(task_id="task-a", base_tree_id=base_tree, store=store)
    assert (recreated.project_root / "tests/api/test_a.py").read_text() == "base\n"
    assert store.load_write_set(write_set.write_set_id) == write_set


def test_from_materialized_root_rejects_missing_manifest(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(WorkspaceError):
        TaskWorkspace.from_materialized_root("task-a", bare, "tree-0")


# ---------------------------------------------------------------------------
# merge_write_sets / apply_tree


def _freeze_change(
    backend: WorkspaceBackend,
    store: TreeStore,
    base_tree: str,
    task_id: str,
    edits: tuple[tuple[str, str | None], ...],
    *patterns: str,
):
    workspace = backend.create(task_id=task_id, base_tree_id=base_tree, store=store)
    for rel, content in edits:
        path = workspace.project_root / rel
        if content is None:
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8")
    return store.freeze_write_set(workspace, claims=_claims(*patterns))


def test_merge_rejects_overlapping_sibling_write_sets(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    set_a = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"),),
        "repo:tests/api/**",
    )
    set_b = _freeze_change(
        backend,
        store,
        base_tree,
        "task-b",
        (("tests/api/test_a.py", "b\n"),),
        "repo:tests/api/**",
    )
    with pytest.raises(WorkspaceError, match="overlap"):
        store.merge_write_sets((set_a, set_b))


def test_merge_rejects_mismatched_base_trees(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    set_a = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"),),
        "repo:tests/api/**",
    )
    (project / "app" / "source.py").write_text("other base\n", encoding="utf-8")
    other_base = store.capture(project)
    set_b = _freeze_change(
        backend,
        store,
        other_base,
        "task-b",
        (("app/source.py", "b\n"),),
        "repo:app/**",
    )
    with pytest.raises(WorkspaceError, match="base"):
        store.merge_write_sets((set_a, set_b))


def test_merge_and_apply_converges_canonical_tree(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / "tests" / "api" / "obsolete.py").write_text("old\n", encoding="utf-8")
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    set_b = _freeze_change(
        backend,
        store,
        base_tree,
        "task-b",
        (("app/source.py", "b\n"),),
        "repo:app/**",
    )
    set_a = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"), ("tests/api/obsolete.py", None)),
        "repo:tests/api/**",
    )
    # merge 按 structural task ID 排序，与传入顺序无关。
    target = store.merge_write_sets((set_b, set_a))
    assert target != base_tree
    # 未 apply 前 canonical 不变。
    assert (project / "tests" / "api" / "test_a.py").read_text() == "base\n"

    store.apply_tree(project, target, base_tree_id=base_tree)
    assert (project / "tests" / "api" / "test_a.py").read_text() == "a\n"
    assert (project / "app" / "source.py").read_text() == "b\n"
    assert not (project / "tests" / "api" / "obsolete.py").exists()
    assert (project / "qa" / "changes" / "CH-1" / "input.txt").read_text() == "case input\n"
    # 幂等：apply 后 canonical capture 与 target tree 一致；重复 apply 是 no-op。
    assert store.capture(project) == target
    store.apply_tree(project, target, base_tree_id=base_tree)
    assert store.capture(project) == target


def test_apply_tree_rejects_canonical_drift(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    write_set = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"),),
        "repo:tests/api/**",
    )
    target = store.merge_write_sets((write_set,))
    # operator 在 commit 之后外部改动 canonical root（target 未触碰的路径）。
    (project / "app" / "source.py").write_text("operator edit\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="drift"):
        store.apply_tree(project, target, base_tree_id=base_tree)
    # drift fail closed：operator 的改动不被覆盖，target 也不部分落盘。
    assert (project / "app" / "source.py").read_text() == "operator edit\n"
    assert (project / "tests" / "api" / "test_a.py").read_text() == "base\n"


def test_apply_tree_cleans_untracked_change_dir_stray(tmp_path: Path) -> None:
    # resume 修复：agent 把声明产物用绝对路径直写进 canonical 的 change 目录
    # （越界残留）。apply_tree 应清理该 untracked 残留而非以 drift 阻断 resume。
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    write_set = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"),),
        "repo:tests/api/**",
    )
    target = store.merge_write_sets((write_set,))
    stray = project / "qa" / "changes" / "CH-1" / "explore" / "advisory.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}\n", encoding="utf-8")

    store.apply_tree(project, target, base_tree_id=base_tree)

    assert not stray.exists()  # 越界残留被清理
    assert not stray.parent.exists()  # 空父目录被回收
    assert (project / "tests" / "api" / "test_a.py").read_text() == "a\n"  # target 正常落盘


def test_apply_tree_rejects_untracked_outside_change_dir(tmp_path: Path) -> None:
    # change 目录之外的 untracked 路径是真实源码漂移，仍须 fail closed。
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    write_set = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"),),
        "repo:tests/api/**",
    )
    target = store.merge_write_sets((write_set,))
    rogue = project / "app" / "rogue.py"
    rogue.write_text("out-of-band source\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="untracked path"):
        store.apply_tree(project, target, base_tree_id=base_tree)
    assert rogue.read_text() == "out-of-band source\n"  # fail closed，不删源码


def test_apply_tree_converges_after_partial_failure(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    backend = _backend(project)
    base_tree = store.capture(project)
    write_set = _freeze_change(
        backend,
        store,
        base_tree,
        "task-a",
        (("tests/api/test_a.py", "a\n"), ("app/source.py", "b\n")),
        "repo:tests/api/**",
        "repo:app/**",
    )
    target = store.merge_write_sets((write_set,))

    real_install = workspace_mod._install_file
    calls = 0

    def fail_second(path: Path, data: bytes, executable: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash mid-apply")
        real_install(path, data, executable)

    monkeypatch.setattr(workspace_mod, "_install_file", fail_second)
    with pytest.raises(OSError, match="simulated crash"):
        store.apply_tree(project, target, base_tree_id=base_tree)
    monkeypatch.setattr(workspace_mod, "_install_file", real_install)

    # partial state 既不是 base 也不是 target，但同一 target 重放必须收敛。
    store.apply_tree(project, target, base_tree_id=base_tree)
    assert (project / "tests" / "api" / "test_a.py").read_text() == "a\n"
    assert (project / "app" / "source.py").read_text() == "b\n"
    assert store.capture(project) == target


# ---------------------------------------------------------------------------
# synchronized live overlays / targeted apply


def _synchronized_issue_claims() -> ResourceClaims:
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    return ResourceClaims(
        reads=synchronized,
        writes=(*synchronized, ResourcePath.parse("change:results/**")),
        synchronized=synchronized,
        exclusive=("project:issue-registry",),
        authorization_writes=(*synchronized, ResourcePath.parse("change:results/**")),
    )


def test_synchronized_overlay_and_targeted_apply_preserve_unrelated_live_tree(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project)

    issue.write_text('{"version":2}\n', encoding="utf-8")
    (project / "app" / "source.py").write_text("live version 2\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be traversed\n", encoding="utf-8")
    os.symlink(outside, project / "app" / "unrelated-live-link")
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="update-issue", base_tree_id=overlay_tree, store=store)

    assert (workspace.project_root / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'
    assert (workspace.project_root / "app/source.py").read_text() == "app base\n"
    assert not (workspace.project_root / "app/unrelated-live-link").exists()
    (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":3}\n', encoding="utf-8")
    result = workspace.change_dir / "results" / "update.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"updated":true}\n', encoding="utf-8")
    write_set = store.freeze_write_set(workspace, claims=_synchronized_issue_claims())
    issue_entry = next(
        entry for entry in write_set.entries if entry.logical_path == "project:qa/issues/ISSUE-1.json"
    )
    assert issue_entry.before_sha256 == hashlib.sha256(b'{"version":2}\n').hexdigest()

    store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)

    assert issue.read_text() == '{"version":3}\n'
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "app/source.py").read_text() == "live version 2\n"
    # Replaying an already-applied targeted update is a no-op.
    store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)


def test_synchronized_overlay_captures_nested_change_ledgers(tmp_path: Path) -> None:
    """A Retro snapshot must retain sibling workflow and Issue ledgers."""
    project = _make_project(tmp_path)
    sibling = project / "qa" / "changes" / "CH-2"
    (sibling / "issues").mkdir(parents=True)
    (sibling / "events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (sibling / "issues" / "events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project)

    synchronized = (ResourcePath.parse("project:qa/changes/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(
        task_id="collect-retro",
        base_tree_id=overlay_tree,
        store=store,
    )

    assert (workspace.project_root / "qa/changes/CH-2/events.jsonl").is_file()
    assert (workspace.project_root / "qa/changes/CH-2/issues/events.jsonl").is_file()
    claims = ResourceClaims(
        reads=synchronized,
        synchronized=synchronized,
        exclusive=("project:retro-evidence-snapshot",),
    )
    assert store.freeze_write_set(workspace, claims=claims).entries == ()


def test_synchronized_targeted_apply_converges_after_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project)
    issue.write_text('{"version":2}\n', encoding="utf-8")
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="update-issue", base_tree_id=overlay_tree, store=store)
    (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":3}\n', encoding="utf-8")
    result = workspace.change_dir / "results" / "update.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"updated":true}\n', encoding="utf-8")
    write_set = store.freeze_write_set(workspace, claims=_synchronized_issue_claims())

    install = workspace_mod._install_file
    calls = 0

    def fail_second(path: Path, data: bytes, executable: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated targeted apply interruption")
        install(path, data, executable)

    monkeypatch.setattr(workspace_mod, "_install_file", fail_second)
    with pytest.raises(OSError, match="targeted apply interruption"):
        store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)
    monkeypatch.setattr(workspace_mod, "_install_file", install)

    store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)
    assert issue.read_text() == '{"version":3}\n'
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'


def test_synchronized_targeted_apply_repairs_content_published_before_executable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text("version 1\n", encoding="utf-8")
    issue.chmod(0o644)
    store = _store(project)
    invocation_tree = store.capture(project)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="update-issue", base_tree_id=overlay_tree, store=store)
    workspace_issue = workspace.project_root / "qa/issues/ISSUE-1.json"
    workspace_issue.write_text("version 2\n", encoding="utf-8")
    workspace_issue.chmod(0o755)
    write_set = store.freeze_write_set(workspace, claims=_synchronized_issue_claims())
    real_install = workspace_mod._install_file

    def publish_content_then_crash(path: Path, data: bytes, executable: bool) -> None:
        assert executable is True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
        raise OSError("simulated crash after replace before chmod")

    monkeypatch.setattr(workspace_mod, "_install_file", publish_content_then_crash)
    with pytest.raises(OSError, match="after replace before chmod"):
        store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)
    monkeypatch.setattr(workspace_mod, "_install_file", real_install)
    assert issue.read_text() == "version 2\n"
    assert issue.stat().st_mode & 0o100 == 0

    store.apply_write_sets_to_synchronized_paths(project, (write_set,), synchronized)

    assert issue.read_text() == "version 2\n"
    assert issue.stat().st_mode & 0o100


def test_synchronized_freeze_rejects_project_write_outside_declared_prefix(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="update-issue", base_tree_id=overlay_tree, store=store)
    (workspace.project_root / "app/source.py").write_text("unauthorized synchronized write\n")
    claims = ResourceClaims(
        reads=synchronized,
        writes=(*synchronized, ResourcePath.parse("project:app/**")),
        synchronized=synchronized,
        exclusive=("project:issue-registry",),
        authorization_writes=(*synchronized, ResourcePath.parse("project:app/**")),
    )

    with pytest.raises(WorkspaceError, match="synchronized write outside declared prefixes"):
        store.freeze_write_set(workspace, claims=claims)


def test_synchronized_freeze_allows_repo_write_when_project_and_repo_roots_alias(
    tmp_path: Path,
) -> None:
    """A repo-scoped test edit must not be reclassified as an unsynchronized project edit."""
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project, repo_root=project)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="assurance", base_tree_id=overlay_tree, store=store)
    (workspace.project_root / "tests/api/test_a.py").write_text("generated\n", encoding="utf-8")
    claims = ResourceClaims(
        reads=synchronized,
        writes=(*synchronized, ResourcePath.parse("repo:tests/api/**")),
        synchronized=synchronized,
        exclusive=("project:issue-registry",),
        authorization_writes=(*synchronized, ResourcePath.parse("repo:tests/api/**")),
    )

    write_set = store.freeze_write_set(workspace, claims=claims)

    assert [entry.logical_path for entry in write_set.entries] == ["project:tests/api/test_a.py"]


def test_synchronized_freeze_does_not_accept_repo_authorization_without_repo_write_claim(
    tmp_path: Path,
) -> None:
    """An aliased authorization alone must not weaken synchronized write isolation."""
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project, repo_root=project)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="assurance", base_tree_id=overlay_tree, store=store)
    (workspace.project_root / "tests/api/test_a.py").write_text("generated\n", encoding="utf-8")
    claims = ResourceClaims(
        reads=synchronized,
        writes=synchronized,
        synchronized=synchronized,
        exclusive=("project:issue-registry",),
        authorization_writes=(*synchronized, ResourcePath.parse("repo:tests/api/**")),
    )

    with pytest.raises(WorkspaceError, match="synchronized write outside declared prefixes"):
        store.freeze_write_set(workspace, claims=claims)


def test_synchronized_freeze_rejects_non_test_repo_write_when_roots_alias(
    tmp_path: Path,
) -> None:
    """The alias exception is only for the repo test namespaces used by assurance."""
    project = _make_project(tmp_path)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = _store(project)
    invocation_tree = store.capture(project, repo_root=project)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    overlay_tree = store.overlay_synchronized_paths(invocation_tree, project, synchronized)
    workspace = _backend(project).create(task_id="assurance", base_tree_id=overlay_tree, store=store)
    (workspace.project_root / "app/source.py").write_text("unauthorized alias write\n", encoding="utf-8")
    claims = ResourceClaims(
        reads=synchronized,
        writes=(*synchronized, ResourcePath.parse("repo:**")),
        synchronized=synchronized,
        exclusive=("project:issue-registry",),
        authorization_writes=(*synchronized, ResourcePath.parse("repo:**")),
    )

    with pytest.raises(WorkspaceError, match="synchronized write outside declared prefixes"):
        store.freeze_write_set(workspace, claims=claims)


# ---------------------------------------------------------------------------
# task-local git 便利索引


def test_workspace_create_does_not_materialize_sibling_retro_dirs(tmp_path: Path) -> None:
    """Narrow read claims must keep sibling Retro runs out of the task workspace."""
    project = _make_project(tmp_path)
    current = project / "qa" / "retro" / "retro-current"
    sibling = project / "qa" / "retro" / "retro-other"
    current.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (current / "context.json").write_text('{"retro_id":"retro-current"}\n', encoding="utf-8")
    (sibling / "context.json").write_text('{"retro_id":"retro-other"}\n', encoding="utf-8")
    (sibling / "secret.md").write_text("leak\n", encoding="utf-8")
    store = _store(project)
    tree_id = store.capture(project)
    claims = ResourceClaims(
        reads=(ResourcePath.parse("project:qa/retro/retro-current/context.json"),),
        writes=(
            ResourcePath.parse("project:qa/retro/retro-current/proposal-candidates.json"),
            ResourcePath.parse("project:qa/retro/retro-current/retro-summary.md"),
        ),
        authorization_writes=(
            ResourcePath.parse("project:qa/retro/retro-current/proposal-candidates.json"),
            ResourcePath.parse("project:qa/retro/retro-current/retro-summary.md"),
        ),
    )

    workspace = _backend(project).create(
        task_id="propose",
        base_tree_id=tree_id,
        store=store,
        claims=claims,
    )

    assert (workspace.project_root / "qa/retro/retro-current/context.json").is_file()
    assert not (workspace.project_root / "qa/retro/retro-other").exists()
    assert not (workspace.project_root / "qa/retro/retro-other/secret.md").exists()
    # Freeze/merge base stays the full overlay tree; sibling omission is materialization-only.
    assert workspace.base_tree_id == tree_id
    # Unclaimed project paths remain in the invocation-base snapshot.
    assert (workspace.project_root / "app/source.py").is_file()
    write_set = store.freeze_write_set(workspace, claims=claims)
    assert write_set.base_tree_id == tree_id
    assert write_set.entries == ()


def test_read_isolated_workspace_materializes_only_claimed_inputs_and_skill_support(
    tmp_path: Path,
) -> None:
    """The Retro proposer must not see mutable ledgers, app code, auth, or memory."""
    project = _make_project(tmp_path)
    current = project / "qa" / "retro" / "retro-current"
    current.mkdir(parents=True)
    (current / "context.json").write_text('{"retro_id":"retro-current"}\n', encoding="utf-8")
    for rel in (
        "qa/archive/CH-OLD/result.json",
        "qa/issues/events.jsonl",
        "qa/improvements/events.jsonl",
        ".aa/memory/aa-run.md",
        ".auth/token",
    ):
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not be visible\n", encoding="utf-8")
    skill = project / "skills" / "aa-retro" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("retro instructions\n", encoding="utf-8")
    store = _store(project)
    tree_id = store.capture(project)
    claims = ResourceClaims(
        reads=(ResourcePath.parse("project:qa/retro/retro-current/context.json"),),
        writes=(ResourcePath.parse("project:qa/retro/retro-current/proposal-candidates.json"),),
        authorization_writes=(ResourcePath.parse("project:qa/retro/retro-current/proposal-candidates.json"),),
    )

    workspace = _backend(project).create(
        task_id="propose",
        base_tree_id=tree_id,
        store=store,
        claims=claims,
        declared_reads_only=True,
        skill_name="aa-retro",
    )

    assert (workspace.project_root / "qa/retro/retro-current/context.json").is_file()
    assert (workspace.project_root / "skills/aa-retro/SKILL.md").is_file()
    assert not (workspace.project_root / "qa/archive").exists()
    assert not (workspace.project_root / "qa/issues").exists()
    assert not (workspace.project_root / "qa/improvements").exists()
    assert not (workspace.project_root / ".aa/memory").exists()
    assert not (workspace.project_root / ".auth").exists()
    assert not (workspace.project_root / "app").exists()
    # Paths omitted by read isolation are not interpreted as mass deletions.
    assert store.freeze_write_set(workspace, claims=claims).entries == ()


@pytest.mark.skipif(_GIT is None, reason="git binary not available")
def test_workspace_git_index_tracks_base_tree(tmp_path: Path) -> None:
    git = _GIT
    assert git is not None  # skipif 已保证；局部变量为 pyright 类型收窄
    project = _make_project(tmp_path)
    store = _store(project)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    assert (workspace.root / ".git").is_dir()

    def diff_names() -> str:
        result = subprocess.run(
            [git, "diff", "--name-only"],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    # 便利索引冻结 base tree：未改动的文件不出现在 worktree-vs-index diff 中。
    assert diff_names() == ""
    (workspace.project_root / "tests" / "api" / "test_a.py").write_text("changed\n")
    assert "tests/api/test_a.py" in diff_names()


def test_git_index_failure_is_contract_error_for_agent_targets(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    base_tree = store.capture(project)

    def fail_run(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["git", "init", "-q"])

    monkeypatch.setattr(workspace_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(workspace_mod.subprocess, "run", fail_run)
    with pytest.raises(WorkspaceError, match="git"):
        _backend(project).create(task_id="task-a", base_tree_id=base_tree, store=store)
    # 显式 side-effect-free 的 builtin target 才允许忽略便利索引失败。
    workspace = _backend(project).create(
        task_id="task-b", base_tree_id=base_tree, store=store, side_effect_free=True
    )
    assert workspace.project_root.is_dir()


def test_missing_git_binary_skips_convenience_index(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    store = _store(project)
    monkeypatch.setattr(workspace_mod.shutil, "which", lambda _name: None)
    workspace = _backend(project).create(task_id="task-a", base_tree_id=store.capture(project), store=store)
    assert workspace.project_root.is_dir()
    assert not (workspace.root / ".git").exists()
