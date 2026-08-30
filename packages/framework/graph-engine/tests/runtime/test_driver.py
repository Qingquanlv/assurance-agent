from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from graph_engine.composition import FrozenComposition
from graph_engine.plugin_api import InvocationWorkspaceBinding
from graph_engine.runtime.driver import StartSpec, acquire_invocation
from graph_engine.runtime.engine import Engine, EngineError
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed


class _RecordingEngine:
    def __init__(self, *, exists: bool) -> None:
        self._exists = exists
        self.open_ids: list[str] = []
        self.start_ids: list[str] = []

    def invocation_exists(self, invocation_id: str) -> bool:
        return self._exists

    def open(self, invocation_id: str, composition: object, **kwargs: object) -> object:
        del composition, kwargs
        self.open_ids.append(invocation_id)
        return object()

    def start(self, composition: object, **kwargs: object) -> object:
        del composition
        self.start_ids.append(str(kwargs["invocation_id"]))
        return object()


def _binding(tmp_path: Path) -> InvocationWorkspaceBinding:
    project = tmp_path / "project"
    attempts = tmp_path / "attempts"
    receipts = tmp_path / "receipts"
    for root in (project, attempts, receipts):
        root.mkdir()
    return InvocationWorkspaceBinding(
        project_root=project,
        attempts_root=attempts,
        receipts_root=receipts,
    )


def _acquire(engine: _RecordingEngine, start: StartSpec | None, tmp_path: Path) -> object:
    return acquire_invocation(
        cast(Engine, engine),
        cast(FrozenComposition, object()),
        invocation_id="inv-1",
        authorization=empty_runtime_authorization(),
        workspace_binding=_binding(tmp_path),
        start=start,
    )


def test_acquire_invocation_starts_when_missing_and_start_is_given(tmp_path: Path) -> None:
    engine = _RecordingEngine(exists=False)
    start = StartSpec(entrypoint="run", seed=empty_invocation_seed())
    _acquire(engine, start, tmp_path)
    assert engine.start_ids == ["inv-1"]
    assert engine.open_ids == []


def test_acquire_invocation_opens_when_present_and_ignores_start(tmp_path: Path) -> None:
    engine = _RecordingEngine(exists=True)
    start = StartSpec(entrypoint="run", seed=empty_invocation_seed())
    _acquire(engine, start, tmp_path)
    assert engine.open_ids == ["inv-1"]
    assert engine.start_ids == []


def test_acquire_invocation_opens_when_present_without_start(tmp_path: Path) -> None:
    engine = _RecordingEngine(exists=True)
    _acquire(engine, None, tmp_path)
    assert engine.open_ids == ["inv-1"]
    assert engine.start_ids == []


def test_acquire_invocation_fail_closes_when_missing_without_start(tmp_path: Path) -> None:
    engine = _RecordingEngine(exists=False)
    with pytest.raises(EngineError, match="invocation is missing"):
        _acquire(engine, None, tmp_path)
    assert engine.open_ids == []
    assert engine.start_ids == []


def test_invocation_exists_is_the_on_disk_namespace(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    try:
        assert engine.invocation_exists("inv-1") is False
        (tmp_path / "invocations" / "inv-1").mkdir()
        assert engine.invocation_exists("inv-1") is True
    finally:
        engine.close()
