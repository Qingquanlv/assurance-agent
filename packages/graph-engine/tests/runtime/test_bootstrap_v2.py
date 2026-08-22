from __future__ import annotations

import contextlib
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import graph_engine.runtime.ledger as ledger_runtime
from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.runtime.engine import Engine, EngineError
from graph_engine.runtime.events import GraphStarted, InvocationStarted, TokenOffered
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed


class InjectedCrash(RuntimeError):
    pass


_BOOTSTRAP_APPEND_BOUNDARIES = (
    "lock_acquired",
    "pending_fsynced",
    "final_installed",
    "directory_fsynced",
)


@pytest.fixture
def composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenComposition:
    source = tmp_path / "source"
    repository = Path(__file__).parents[4]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    declaration_path = source / "graph_engine_toy_a" / "product-declaration.json"
    declaration = json.loads(declaration_path.read_bytes())
    declaration["manifest"]["entrypoints"] = {"full": "root"}
    declaration["manifest"]["workflow"]["entrypoints"] = {"full": "root"}
    declaration_path.write_bytes(canonical_json_bytes(declaration))
    provider_path = source / "graph_engine_toy_a" / "product.py"
    provider_path.write_text(
        provider_path.read_text(encoding="utf-8")
        .replace('entrypoints={"hello": "root"}', 'entrypoints={"full": "root"}')
        .replace('"entrypoints": {"hello": "root"}', '"entrypoints": {"full": "root"}'),
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
    common: dict[str, object] = {
        "distribution": "graph-engine-toy-a",
        "entrypoint_name": "toy-a",
        "source_root": source,
        "source_files": source_files,
    }
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                **common,
                declaration_path="graph_engine_toy_a/product-declaration.json",
            ),
            plugins=(
                EditableWheelPluginSource(
                    **common,
                    declaration_path="graph_engine_toy_a/plugin-declaration.json",
                ),
            ),
        )
    )


@pytest.fixture
def seed() -> object:
    return empty_invocation_seed()


@pytest.fixture
def engine_factory(tmp_path: Path):
    real_boundary = ledger_runtime._append_boundary

    def factory(*, fail_after_bootstrap_append: int | None = None) -> Engine:
        root = tmp_path / "engine"
        engine = Engine(root)
        if fail_after_bootstrap_append is None:
            ledger_runtime._append_boundary = real_boundary
            return engine
        cut = fail_after_bootstrap_append
        seen = {"count": 0}

        def crash_boundary(name: str) -> None:
            real_boundary(name)
            if name not in _BOOTSTRAP_APPEND_BOUNDARIES:
                return
            if seen["count"] == cut:
                raise InjectedCrash(f"simulated bootstrap append cut at {name}")
            seen["count"] += 1

        ledger_runtime._append_boundary = crash_boundary
        return engine

    yield factory
    ledger_runtime._append_boundary = real_boundary


@pytest.mark.parametrize("boundary", range(4))
def test_start_recovers_to_one_authenticated_bootstrap_prefix(
    engine_factory,
    composition: FrozenComposition,
    seed: object,
    boundary: int,
) -> None:
    engine = engine_factory(fail_after_bootstrap_append=boundary)
    with contextlib.suppress(InjectedCrash):
        engine.start(composition, entrypoint="full", invocation_id="inv-1", seed=seed)
    recovered_engine = engine_factory()
    try:
        recovered = recovered_engine.open("inv-1", composition)
    except EngineError:
        recovered = recovered_engine.start(
            composition,
            entrypoint="full",
            invocation_id="inv-1",
            seed=seed,
        )
    events = recovered.ledger.read_all()
    assert [item.event.kind for item in events[:3]] == [
        "invocation_started",
        "graph_started",
        "token_offered",
    ]
    assert len([item for item in events if item.event.kind == "invocation_started"]) == 1
    started = events[0].event
    assert isinstance(started, InvocationStarted)
    assert started.event_schema_version == "2"
    assert started.runtime_authorization_digest == EMPTY_RUNTIME_AUTHORIZATION_DIGEST
    graph = events[1].event
    token = events[2].event
    assert isinstance(graph, GraphStarted)
    assert isinstance(token, TokenOffered)
    assert graph.input == token.payload
    recovered.close()
    recovered_engine.close()


def test_open_rejects_schema_v1_prototype_invocation(
    tmp_path: Path,
    composition: FrozenComposition,
    seed: object,
) -> None:
    root = tmp_path / "engine"
    with Engine(root) as engine:
        engine.start(composition, entrypoint="full", invocation_id="legacy", seed=seed).close()
    invocation = root / "invocations" / "legacy"
    intent_path = invocation / "invocation.start.json"
    os.chmod(intent_path, 0o600)
    legacy_intent = {
        "schema_version": "1",
        "lock_digest": composition.lock_digest,
        "entrypoint": "full",
    }
    intent_path.write_bytes(canonical_json_bytes(legacy_intent))
    intent_path.chmod(0o400)
    with Engine(root) as engine, pytest.raises(Exception, match="schema-v1 prototype"):
        engine.open("legacy", composition)
