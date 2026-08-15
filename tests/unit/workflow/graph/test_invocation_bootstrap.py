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
