from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.status import (
    read_run_manifest,
    write_bootstrap_status,
    write_run_manifest,
)
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.operator import AssuranceOperator, OperatorError
from assurance_product.task_records import active_manifest, define_task, prepare_managed_workspace


def _managed_pair(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    task = tmp_path / "task"
    task.mkdir()
    definition = define_task(
        project_dir=tmp_path,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )
    previous = task / ".aa" / "runs" / "BOOT-old"
    current = task / ".aa" / "runs" / "BOOT-new"
    for run_dir in (previous, current):
        write_run_manifest(
            run_dir,
            {
                "source_project_dir": str(tmp_path),
                "project_dir": str(task),
                "worktree": str(task),
                "task_directory": str(task),
                "task_id": definition.task_id,
                "change_id": run_dir.name,
                "invocation_id": run_dir.name,
            },
        )
    write_bootstrap_status(
        previous,
        BootstrapStatusV1(phase="terminal", change_id="BOOT-old", exit_code=0),
    )
    write_bootstrap_status(current, BootstrapStatusV1(phase="preparing", change_id="BOOT-new"))
    ChangeWorkspace.prepare(task, "BOOT-old")
    content = b'{"change": {"change_id": "BOOT-old", "state": "achieved"}}\n'
    (task / "qa" / "status.json").write_bytes(content)
    return task, previous, current, content


def test_next_managed_run_archives_prior_status_and_preserves_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, previous, current, content = _managed_pair(tmp_path)
    retained = (
        task / "qa" / ".runtime" / "receipts" / "prior-receipt.json",
        task / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3",
        task / "qa" / "cases" / "case.yaml",
        previous / "attempts.json",
    )
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"prior evidence\n")
    monkeypatch.setattr("assurance_product.operator.launch_worker", lambda **kwargs: None)

    AssuranceOperator()._launch_or_fail(run_dir=current, change_id="BOOT-new", environ={})

    assert (previous / "qa-status.json").read_bytes() == content
    assert not (task / "qa" / "status.json").exists()
    assert ChangeWorkspace.open(task, "BOOT-new").change_id == "BOOT-new"
    assert all(path.read_bytes() == b"prior evidence\n" for path in retained)


@pytest.mark.parametrize(
    "problem",
    [
        "unknown",
        "active",
        "unknown_exit",
        "wrong_task",
        "wrong_directory",
        "wrong_status",
        "archive",
        "archive_symlink",
    ],
)
def test_rollover_rejects_unproven_ownership_without_moving_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    task, previous, current, content = _managed_pair(tmp_path)
    if problem == "unknown":
        (previous / "run-manifest.json").unlink()
    elif problem in {"active", "unknown_exit", "wrong_status"}:
        write_bootstrap_status(
            previous,
            BootstrapStatusV1(
                phase="running" if problem == "active" else "terminal",
                change_id="some-other-run" if problem == "wrong_status" else "BOOT-old",
                exit_code=None if problem == "unknown_exit" else 0,
            ),
        )
    elif problem in {"wrong_task", "wrong_directory"}:
        manifest = read_run_manifest(previous)
        manifest["task_id" if problem == "wrong_task" else "task_directory"] = "some-other-task"
        write_run_manifest(previous, manifest)
    elif problem == "archive_symlink":
        (previous / "qa-status.json").symlink_to(task / "qa" / "status.json")
    else:
        (previous / "qa-status.json").write_bytes(b"existing history\n")
    monkeypatch.setattr("assurance_product.operator.launch_worker", lambda **kwargs: None)

    with pytest.raises(OperatorError):
        AssuranceOperator()._launch_or_fail(run_dir=current, change_id="BOOT-new", environ={})

    assert (task / "qa" / "status.json").read_bytes() == content
    if problem == "archive":
        assert (previous / "qa-status.json").read_bytes() == b"existing history\n"
    elif problem == "archive_symlink":
        assert (previous / "qa-status.json").is_symlink()


def test_unmanaged_workspace_still_rejects_another_change(tmp_path: Path) -> None:
    task, _previous, _current, content = _managed_pair(tmp_path)

    with pytest.raises(ValueError, match="identity does not match"):
        ChangeWorkspace.prepare(task, "unmanaged-new")

    assert json.loads((task / "qa" / "status.json").read_bytes()) == json.loads(content)


def test_rollover_recovers_after_archiving_before_removing_current_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, previous, _current, content = _managed_pair(tmp_path)
    status_path = task / "qa" / "status.json"
    unlink = Path.unlink

    def fail_removal(path: Path, *args, **kwargs) -> None:
        if path == status_path:
            raise OSError("interrupted after archive")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_removal)
        with pytest.raises(OSError, match="interrupted after archive"):
            prepare_managed_workspace(task, "BOOT-new")
    assert (previous / "qa-status.json").read_bytes() == content
    assert status_path.read_bytes() == content

    workspace = prepare_managed_workspace(task, "BOOT-new")

    assert workspace.change_id == "BOOT-new"
    assert (previous / "qa-status.json").read_bytes() == content
    assert not status_path.exists()


@pytest.mark.parametrize("graph_status", ["blocked", "interrupted", "running", None])
def test_terminal_worker_keeps_ownership_of_a_nonterminal_graph(
    tmp_path: Path, graph_status: str | None
) -> None:
    task, previous, _current, content = _managed_pair(tmp_path)
    graph = {"status": graph_status} if graph_status else {}
    write_bootstrap_status(
        previous,
        BootstrapStatusV1(phase="terminal", change_id="BOOT-old", exit_code=20, status=graph),
    )
    if graph_status is None:
        identity = task / "qa" / ".runtime" / "langgraph" / "identities" / "BOOT-old.json"
        identity.parent.mkdir(parents=True)
        identity.write_text('{"phase": "initialized"}', encoding="utf-8")
    manifest = {**read_run_manifest(previous), "run_dir": str(previous)}

    assert active_manifest([manifest]) == manifest
    with pytest.raises(ValueError, match="terminal"):
        prepare_managed_workspace(task, "BOOT-new")

    assert (task / "qa" / "status.json").read_bytes() == content
    assert not (previous / "qa-status.json").exists()
