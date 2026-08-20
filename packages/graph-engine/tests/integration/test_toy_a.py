from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from graph_engine.composition.sources import WheelPluginDeclaration, WheelProductDeclaration
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.product import load_plugin_entrypoint, load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine


def test_toy_a_static_declarations_match_live_providers() -> None:
    package = files("graph_engine_toy_a")
    product = load_product_entrypoint("toy-a")
    plugin = load_plugin_entrypoint("toy-a")

    product_declaration = WheelProductDeclaration.model_validate_json(
        package.joinpath("product-declaration.json").read_bytes()
    )
    plugin_declaration = WheelPluginDeclaration.model_validate_json(
        package.joinpath("plugin-declaration.json").read_bytes()
    )

    assert product_declaration.manifest == product.manifest()
    assert plugin_declaration.descriptor == plugin.descriptor()


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    def __init__(self, *, fail_first_greet: bool = False) -> None:
        self.executions = 0
        self._fail_first_greet = fail_first_greet

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        self.executions += 1
        if self._fail_first_greet and self.executions == 1:
            return TaskOutcome.failed("transient", "retry the toy greeting")
        return await handler.execute(
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


def test_toy_a_retries_a_transient_first_greet_attempt(tmp_path: Path) -> None:
    product = load_product_entrypoint("toy-a")
    plugin = load_plugin_entrypoint("toy-a")
    resolved = resolve_product(product, {"toy.a": plugin})
    host = _InProcessTestHost(fail_first_greet=True)

    with Engine(tmp_path / "engine", host=host) as engine:
        with engine.start(resolved, entrypoint="hello", invocation_id="toy-a-retry") as handle:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded", result
            assert result.output == {"message": "hello Ada"}
            assert host.executions == 2
            greet = next(
                activation for activation in result.projection.activations if activation.node_id == "greet"
            )
            assert tuple(attempt.attempt for attempt in greet.attempts) == (1, 2)
            assert greet.attempts[0].failure is not None
            assert greet.attempts[0].failure.kind == "transient"
            assert greet.attempts[1].status == "succeeded"
            with handle.workspace as workspace:
                assert workspace.read_head("greeting.txt") == b"hello Ada\n"
