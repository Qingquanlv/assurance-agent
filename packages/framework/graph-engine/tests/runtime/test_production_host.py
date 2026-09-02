from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import InvocationWorkspaceBinding
from graph_engine.runtime.engine import Engine
from graph_engine.attempts.production_host import UnsupportedProductionPlatform
from graph_engine.attempts.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed


@pytest.fixture
def installed_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenComposition:
    source = tmp_path / "source"
    shutil.copytree(
        Path(__file__).resolve().parent / "fixtures" / "leftover-toy-a",
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
def authorization() -> object:
    return empty_runtime_authorization()


def _workspace_binding(tmp_path: Path) -> InvocationWorkspaceBinding:
    project_root = tmp_path / "project"
    attempts_root = tmp_path / "attempts"
    receipts_root = tmp_path / "promotion-receipts"
    for root in (project_root, attempts_root, receipts_root):
        root.mkdir()
    return InvocationWorkspaceBinding(project_root, attempts_root, receipts_root)


def test_production_engine_executes_installed_handler(
    tmp_path: Path,
    installed_composition: FrozenComposition,
    seed: object,
    authorization: object,
) -> None:
    engine = Engine.production(tmp_path, authorization=authorization)
    workspace_binding = _workspace_binding(tmp_path)
    try:
        handle = engine.start(
            installed_composition,
            entrypoint="full",
            invocation_id="inv",
            seed=seed,
            authorization=authorization,
            workspace_binding=workspace_binding,
        )
        try:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded"
        finally:
            handle.close()
    finally:
        engine.close()


def test_production_engine_rejects_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authorization: object,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedProductionPlatform, match="Linux and macOS"):
        Engine.production(tmp_path, authorization=authorization)
