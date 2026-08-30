from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.engine import Engine, _UnavailableTaskHost
from graph_engine.__main__ import _TrustedWheelPluginHost


class _RecoverableHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded()

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


def test_cli_host_refuses_recoverable_handlers() -> None:
    host = _TrustedWheelPluginHost()
    with pytest.raises(GraphEngineError, match="refuses recoverable handlers"):
        host.bind_invocation_runtime(
            handlers={"test.empty.run": _RecoverableHandler()},
            store=object(),
        )


def test_engine_default_remains_unavailable_task_host(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    try:
        assert engine._host is None
        assert isinstance(engine._host or _UnavailableTaskHost(), _UnavailableTaskHost)
    finally:
        engine.close()
