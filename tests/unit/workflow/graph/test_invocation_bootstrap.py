"""Shared invocation start bootstrap: started event + pins + runtime meta."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.workflow.graph.invocation_bootstrap import write_invocation_runtime_meta
from assurance_agent.workflow.graph.models import RuntimeContext


class _FakeTxn:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def write_runtime_file(self, relpath: str, data: bytes) -> None:
        self.files[relpath] = data


def test_write_invocation_runtime_meta_pins_host_paths(tmp_path: Path) -> None:
    txn = _FakeTxn()
    context = RuntimeContext(
        project_root=tmp_path / "sut",
        repo_root=tmp_path / "sut",
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        parent_session_id="sess-1",
    )
    write_invocation_runtime_meta(txn, "inv-1", context, extra={"parent_task_id": "t-1"})
    payload = json.loads(txn.files[".graph-runtime/invocations/inv-1.json"])
    assert payload["change_id"] == "CH-1"
    assert payload["parent_session_id"] == "sess-1"
    assert payload["parent_task_id"] == "t-1"
    assert payload["project_root"] == str(tmp_path / "sut")
    assert payload["host_project_root"] == str(tmp_path / "sut")


def test_write_invocation_runtime_meta_keeps_host_root_when_project_root_is_workspace(
    tmp_path: Path,
) -> None:
    txn = _FakeTxn()
    host = tmp_path / "sut"
    workspace = tmp_path / "qa" / "changes" / "CH-1" / ".graph-runtime" / "tasks" / "t1"
    context = RuntimeContext(
        project_root=workspace,
        repo_root=workspace,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        host_project_root=host,
    )
    write_invocation_runtime_meta(txn, "inv-child", context)
    payload = json.loads(txn.files[".graph-runtime/invocations/inv-child.json"])
    assert payload["project_root"] == str(workspace)
    assert payload["host_project_root"] == str(host)


def test_runtime_context_from_meta_restores_host_project_root(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.invocation_bootstrap import runtime_context_from_meta

    host = tmp_path / "sut"
    change_dir = host / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    workspace = change_dir / ".graph-runtime" / "tasks" / "t1"
    ctx = runtime_context_from_meta(
        change_dir=change_dir,
        params={"k": 1},
        meta={
            "project_root": str(workspace),
            "repo_root": str(workspace),
            "change_id": "CH-1",
            "parent_session_id": "sess",
            "host_project_root": str(host),
        },
    )
    assert ctx.project_root == workspace
    assert ctx.repo_root == workspace
    assert ctx.host_project_root == host
    assert ctx.change_dir == change_dir
    assert ctx.params == {"k": 1}


def test_runtime_context_from_meta_infers_host_when_legacy_child_meta_omits_it(
    tmp_path: Path,
) -> None:
    from assurance_agent.workflow.graph.invocation_bootstrap import runtime_context_from_meta

    host = tmp_path / "sut"
    change_dir = host / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    workspace = change_dir / ".graph-runtime" / "tasks" / "t1"
    ctx = runtime_context_from_meta(
        change_dir=change_dir,
        params={},
        meta={
            "project_root": str(workspace),
            "repo_root": str(workspace),
            "change_id": "CH-1",
        },
    )
    assert ctx.project_root == workspace
    assert ctx.host_project_root == host


def test_child_context_copy_preserves_host_project_root(tmp_path: Path) -> None:
    host = tmp_path / "sut"
    change = host / "qa" / "changes" / "CH-1"
    workspace = change / ".graph-runtime" / "tasks" / "t1"
    parent = RuntimeContext(
        project_root=host,
        repo_root=host,
        change_dir=change,
        change_id="CH-1",
        host_project_root=host,
        params={"run_mode": "full"},
    )
    child = parent.model_copy(update={"project_root": workspace, "repo_root": workspace})
    assert child.project_root == workspace
    assert child.host_project_root == host
    assert child.change_dir == change
