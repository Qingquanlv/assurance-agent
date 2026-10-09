from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.attempts.resources.workspace import TaskWorkspaceStore, TaskWorkspaceViolation


class _PromotionCrash(RuntimeError):
    pass


def test_recovery_consumes_durable_promotion_without_reexecuting_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="work", attempt=1, output_paths=("out.txt",))

    executions += 1
    (binding.write_root / "out.txt").write_bytes(b"durable")
    staged = store.seal(binding.identity)
    assert executions == 1

    real_replace = __import__("os").replace

    def crash_after_first_replace(
        source: str | bytes | Path, target: str | bytes | Path, *args: object, **kwargs: object
    ) -> None:
        real_replace(source, target, *args, **kwargs)
        raise _PromotionCrash("after_promotion")

    monkeypatch.setattr("graph_engine.attempts.resources.workspace.os.replace", crash_after_first_replace)
    with pytest.raises(_PromotionCrash, match="after_promotion"):
        store.promote(binding.identity, staged)
    monkeypatch.undo()

    assert executions == 1
    receipt = store.promote(binding.identity, staged)
    assert executions == 1
    assert receipt.identity_digest == binding.identity.identity_digest
    assert (project / "out.txt").read_bytes() == b"durable"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()
    (binding.write_root / "out.txt").write_bytes(b"different")
    with pytest.raises(TaskWorkspaceViolation):
        store.promote(binding.identity, store.seal(binding.identity))
    assert (project / "out.txt").read_bytes() == b"durable"
