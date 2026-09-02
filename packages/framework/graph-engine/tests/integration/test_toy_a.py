from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from graph_engine.boot.generic import (
    boot_factory_product,
    contract_resolver_from_plugins,
    invocation_values,
    run_factory_product,
    workspace_provider_for,
)
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.composition.lock import ProductLock


def _toy_a_composition(root: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[5]
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
    return RegistryPlatform().resolve(
        ResolutionRequest(
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
    )


def test_toy_a_static_declarations_match_live_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path, monkeypatch)
    assert composition.manifest.product_id == "toy.a"
    assert composition.manifest.graph_factory_symbol == "graph_engine_toy_a.product:build_toy_a_graphs"
    assert composition.workflow is None
    assert isinstance(composition.lock, ProductLock)
    assert tuple(descriptor.plugin_id for descriptor in composition.descriptors) == ("toy.a",)


async def test_toy_a_runs_without_assurance_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)
    workspace, project_root = workspace_provider_for(tmp_path / "engine")
    resolver = contract_resolver_from_plugins(resolved.descriptors, workspace)
    artifact, kernel = boot_factory_product(
        resolved,
        workspace=workspace,
        contract_resolver=resolver,
    )
    result = await run_factory_product(
        artifact,
        kernel=kernel,
        workspace=workspace,
        entrypoint="hello",
        invocation_id="toy-a-1",
        graph_input={"name": "Ada"},
        lease_root=tmp_path / "leases",
    )
    values = await invocation_values(artifact, "toy-a-1", "hello")
    assert result.status == "completed", result
    assert values.get("message") == "hello Ada"
    assert (project_root / "greeting.txt").read_bytes() == b"hello Ada\n"


async def test_toy_a_retries_a_transient_first_greet_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_a_composition(tmp_path / "composition", monkeypatch)
    workspace, project_root = workspace_provider_for(tmp_path / "engine")
    resolver = contract_resolver_from_plugins(resolved.descriptors, workspace, fail_first=True)
    executor = resolver.executors["toy.a.greet"].executor
    assert type(executor).__name__ == "GreetExecutor"
    artifact, kernel = boot_factory_product(
        resolved,
        workspace=workspace,
        contract_resolver=resolver,
    )
    first = await run_factory_product(
        artifact,
        kernel=kernel,
        workspace=workspace,
        entrypoint="hello",
        invocation_id="toy-a-retry-1",
        graph_input={"name": "Ada"},
        lease_root=tmp_path / "leases-1",
    )
    assert first.status == "completed", first
    first_values = await invocation_values(artifact, "toy-a-retry-1", "hello")
    assert first_values.get("attempt_failure") is not None
    second = await run_factory_product(
        artifact,
        kernel=kernel,
        workspace=workspace,
        entrypoint="hello",
        invocation_id="toy-a-retry-2",
        graph_input={"name": "Ada"},
        lease_root=tmp_path / "leases-2",
    )
    values = await invocation_values(artifact, "toy-a-retry-2", "hello")
    assert second.status == "completed", second
    assert values.get("message") == "hello Ada"
    assert executor.executions == 2
    assert (project_root / "greeting.txt").read_bytes() == b"hello Ada\n"
