from __future__ import annotations

from collections.abc import Callable
import importlib
from pathlib import Path
import shutil
import sys

import pytest

from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.runtime.engine import Engine


def _toy_a_composition(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[4]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_a" or module_name.startswith("graph_engine_toy_a."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    request = ResolutionRequest(
        product=EditableWheelProductSource(
            distribution="graph-engine-toy-a",
            entrypoint_name="toy-a",
            declaration_path="graph_engine_toy_a/product-declaration.json",
            source_root=source,
            source_files=source_files,
        ),
        plugins=(
            EditableWheelPluginSource(
                distribution="graph-engine-toy-a",
                entrypoint_name="toy-a",
                declaration_path="graph_engine_toy_a/plugin-declaration.json",
                source_root=source,
                source_files=source_files,
            ),
        ),
    )
    return RegistryPlatform().resolve(request)


def test_toy_a_static_declarations_match_live_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path, monkeypatch)

    assert composition.manifest.product_id == "toy.a"
    assert tuple(descriptor.plugin_id for descriptor in composition.descriptors) == ("toy.a",)


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


def test_toy_a_runs_without_assurance_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)

    with Engine(tmp_path / "engine", host=_InProcessTestHost()) as engine:
        with engine.start(resolved, entrypoint="hello", invocation_id="toy-a-1") as handle:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded", result
            assert result.output == {"message": "hello Ada"}
            with handle.workspace as workspace:
                assert workspace.read_head("greeting.txt") == b"hello Ada\n"


def test_toy_a_retries_a_transient_first_greet_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)
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
