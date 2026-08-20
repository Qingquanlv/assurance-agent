from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.product import load_plugin_entrypoint, load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        return await handler(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


def test_toy_a_runs_without_assurance_packages(tmp_path: Path) -> None:
    product = load_product_entrypoint("toy-a")
    plugin = load_plugin_entrypoint("toy-a")
    resolved = resolve_product(product, {"toy.a": plugin})

    with Engine(tmp_path / "engine", host=_InProcessTestHost()) as engine:
        with engine.start(resolved, entrypoint="hello", invocation_id="toy-a-1") as handle:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded", result
            assert result.output == {"message": "hello Ada"}
            with handle.workspace as workspace:
                assert workspace.read_head("greeting.txt") == b"hello Ada\n"
