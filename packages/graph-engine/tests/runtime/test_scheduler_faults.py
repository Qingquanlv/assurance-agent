from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from graph_engine.plugin_api import ResourceClaims, TaskContext, TaskOutcome, TaskRequest
from graph_engine.runtime.task_workspace import PromotionPublicationIndeterminate

from test_scheduler import _dual_root_scheduler, _task


def test_indeterminate_handler_exception_never_promotes_partial_stage(tmp_path: Path) -> None:
    task = _task("work", resources=ResourceClaims(writes=("out.txt",)))

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "out.txt").write_bytes(b"partial")
        raise RuntimeError("worker transport became indeterminate")

    scheduler, store, _ledger, _host, project_root = _dual_root_scheduler(
        tmp_path,
        {task.capability_id: handler},
    )
    try:
        (result,) = asyncio.run(scheduler.run_wave((task,)))
    finally:
        store.close()

    assert result.outcome.status == "failed"
    assert not (project_root / "out.txt").exists()


def test_promotion_publication_indeterminate_remains_prepared_not_ordinary_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("work", resources=ResourceClaims(writes=("out.txt",)))

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "out.txt").write_bytes(b"candidate")
        return TaskOutcome.succeeded({"ok": True})

    scheduler, store, ledger, _host, _project_root = _dual_root_scheduler(
        tmp_path,
        {task.capability_id: handler},
    )

    def indeterminate(*_args: object, **_kwargs: object) -> object:
        raise PromotionPublicationIndeterminate("injected publication uncertainty")

    monkeypatch.setattr(store, "promote", indeterminate)
    try:
        with pytest.raises(PromotionPublicationIndeterminate, match="uncertainty"):
            asyncio.run(scheduler.run_wave((task,)))
    finally:
        store.close()

    kinds = [envelope.event.kind for envelope in ledger.read_all()]
    assert "task_commit_prepared" in kinds
    assert "task_promotion_completed" not in kinds
    assert "task_attempt_failed" not in kinds
