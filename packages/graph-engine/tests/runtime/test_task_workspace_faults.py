from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.runtime.task_workspace import TaskWorkspaceStore, TaskWorkspaceViolation


def test_partial_promotion_failure_leaves_a_durable_pending_record_and_refuses_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task", attempt=1, output_paths=("left.txt", "right.txt"))
    (binding.write_root / "left.txt").write_bytes(b"left")
    (binding.write_root / "right.txt").write_bytes(b"right")
    staged = store.seal(binding.identity)

    real_replace = __import__("os").replace
    calls = 0

    def fail_second_replace(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected second replace failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.runtime.task_workspace.os.replace", fail_second_replace)

    with pytest.raises(OSError, match="injected"):
        store.promote(binding.identity, staged)

    assert (project / "left.txt").read_bytes() == b"left"
    assert not (project / "right.txt").exists()
    assert (store.receipts_root / f".{binding.identity.identity_digest}.pending.json").is_file()
    with pytest.raises(TaskWorkspaceViolation, match="incomplete"):
        store.promote(binding.identity, staged)
