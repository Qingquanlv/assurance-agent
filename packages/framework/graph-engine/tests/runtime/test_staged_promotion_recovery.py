from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bootstrap_fixtures import leftover_scheduler as scheduler_runtime
from graph_engine.plugin_api import ResourceClaims, TaskContext, TaskOutcome, TaskRequest

from test_scheduler import _dual_root_scheduler, _task


class _PromotionCrash(RuntimeError):
    pass


def test_recovery_consumes_durable_promotion_without_reexecuting_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("work", resources=ResourceClaims(writes=("out.txt",)))
    executions = 0

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal executions
        executions += 1
        (context.write_root / "out.txt").write_bytes(b"durable")
        return TaskOutcome.succeeded({"ok": True})

    scheduler, store, ledger, _host, project_root = _dual_root_scheduler(
        tmp_path,
        {task.capability_id: handler},
    )

    def crash_after_promotion(name: str) -> None:
        if name == "after_promotion":
            raise _PromotionCrash(name)

    monkeypatch.setattr(scheduler_runtime, "_promotion_cut", crash_after_promotion)
    try:
        with pytest.raises(_PromotionCrash, match="after_promotion"):
            asyncio.run(scheduler.run_wave((task,)))
        assert executions == 1
        assert (project_root / "out.txt").read_bytes() == b"durable"
        assert "task_commit_prepared" in [item.event.kind for item in ledger.read_all()]
        assert "task_promotion_completed" not in [item.event.kind for item in ledger.read_all()]

        monkeypatch.setattr(scheduler_runtime, "_promotion_cut", lambda _name: None)
        (result,) = asyncio.run(scheduler.resume_running((task,)))
    finally:
        store.close()

    assert executions == 1
    assert result.outcome.status == "succeeded"
    terminal = next(item.event for item in ledger.read_all() if item.event.kind == "task_promotion_completed")
    payload = terminal.model_dump(mode="json")
    assert payload["staged_write_set_digest"]
    assert payload["promotion_receipt_digest"]
    assert "candidate_tree_id" not in payload
    assert "current_head_tree_id" not in payload
