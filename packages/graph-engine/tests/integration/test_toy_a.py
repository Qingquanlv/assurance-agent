from __future__ import annotations

from collections.abc import Mapping
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
from graph_engine.plugin_api import InvocationWorkspaceBinding, TaskContext, TaskHandler, TaskOutcome
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostExecuteCall
from graph_engine.runtime.task_workspace import TaskWorkspaceStore


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
    plugin_path = source / "graph_engine_toy_a" / "plugin.py"
    plugin_path.write_text(
        plugin_path.read_text(encoding="utf-8").replace("context.workspace_root", "context.write_root"),
        encoding="utf-8",
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
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: TaskWorkspaceStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        self.executions += 1
        if self._fail_first_greet and self.executions == 1:
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.failed("transient", "retry the toy greeting"),
            )
        handler = self._handlers[call.request.capability_id]
        identity = call.attempt_root.workspace_identity
        binding = self._store.begin(
            task_id=identity.task_id,
            attempt=identity.attempt,
            output_paths=identity.output_paths,
        )
        assert binding.identity == identity
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=binding.project_root,
                write_root=binding.write_root,
                workspace_identity=binding.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)


def _workspace_binding(root: Path) -> InvocationWorkspaceBinding:
    project_root = root.parent / f".{root.name}-project"
    attempts_root = root.parent / f".{root.name}-attempts"
    receipts_root = root.parent / f".{root.name}-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


def test_toy_a_runs_without_assurance_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)
    engine_root = tmp_path / "engine"
    workspace_binding = _workspace_binding(engine_root)

    with Engine(engine_root, host=_InProcessTestHost()) as engine:
        with engine.start(
            resolved,
            entrypoint="hello",
            invocation_id="toy-a-1",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        ) as handle:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded", result
            assert result.output == {"message": "hello Ada"}
            assert (workspace_binding.project_root / "greeting.txt").read_bytes() == b"hello Ada\n"


def test_toy_a_retries_a_transient_first_greet_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)
    host = _InProcessTestHost(fail_first_greet=True)
    engine_root = tmp_path / "engine"
    workspace_binding = _workspace_binding(engine_root)

    with Engine(engine_root, host=host) as engine:
        with engine.start(
            resolved,
            entrypoint="hello",
            invocation_id="toy-a-retry",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        ) as handle:
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
            assert (workspace_binding.project_root / "greeting.txt").read_bytes() == b"hello Ada\n"
