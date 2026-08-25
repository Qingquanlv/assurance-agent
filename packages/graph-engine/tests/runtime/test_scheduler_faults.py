from __future__ import annotations

import asyncio
from pathlib import Path

from graph_engine.plugin_api import ResourceClaims, TaskContext, TaskOutcome, TaskRequest

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
