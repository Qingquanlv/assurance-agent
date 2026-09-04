from __future__ import annotations

import hashlib
import importlib
import shutil
import sys
from pathlib import Path

import pytest

from graph_engine.boot.generic import (
    boot_factory_product,
    contract_resolver_from_plugins,
    factory_application,
    invocation_values,
    workspace_provider_for,
)
from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.composition.lock import ProductLock


def _toy_composition(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    toy: str,
) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[5]
    distribution = f"graph-engine-toy-{toy}"
    package_name = f"graph_engine_toy_{toy}"
    entrypoint_name = f"toy-{toy}"
    shutil.copytree(
        repository / "examples" / distribution,
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                distribution=distribution,
                entrypoint_name=entrypoint_name,
                declaration_path=f"{package_name}/product-declaration.json",
                source_root=source,
                source_files=source_files,
            ),
            plugins=(
                EditableWheelPluginSource(
                    distribution=distribution,
                    entrypoint_name=entrypoint_name,
                    declaration_path=f"{package_name}/plugin-declaration.json",
                    source_root=source,
                    source_files=source_files,
                ),
            ),
        )
    )


def _project_digest(project_root: Path) -> str:
    return canonical_digest(
        {
            path.relative_to(project_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(project_root.rglob("*"))
            if path.is_file()
        }
    )


async def _run_to_completion(
    root: Path,
    composition: FrozenComposition,
    *,
    invocation_id: str,
) -> tuple[str, str, dict[str, object]]:
    workspace, project_root = workspace_provider_for(root)
    resolver = contract_resolver_from_plugins(composition.descriptors, workspace)
    artifact, kernel = boot_factory_product(
        composition,
        workspace=workspace,
        contract_resolver=resolver,
    )
    application, factory = factory_application(
        artifact,
        kernel=kernel,
        workspace=workspace,
        lease_root=root / "leases",
    )
    blocked = await application.start_and_run(
        invocation_id=invocation_id,
        entrypoint="review",
        graph_input={},
        execution_factory=factory,
    )
    assert blocked.status == "interrupted", blocked
    completed = await application.resume(
        invocation_id=invocation_id,
        execution_factory=factory,
        resume="approve",
    )
    assert completed.status == "completed", completed
    values = dict(await invocation_values(artifact, invocation_id, "review"))
    return composition.lock_digest, _project_digest(project_root), values


def test_toy_b_static_declarations_match_live_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_composition(tmp_path, monkeypatch, "b")
    assert composition.manifest.product_id == "toy.b"
    assert composition.manifest.graph_factory_symbol == "graph_engine_toy_b.product:build_toy_b_graphs"
    assert not hasattr(composition, "workflow")
    assert isinstance(composition.lock, ProductLock)
    assert tuple(descriptor.plugin_id for descriptor in composition.descriptors) == ("toy.b",)


async def test_toy_b_recovers_then_interrupts_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_composition(tmp_path / "composition", monkeypatch, "b")
    workspace, project_root = workspace_provider_for(tmp_path / "engine")
    resolver = contract_resolver_from_plugins(resolved.descriptors, workspace)
    artifact, kernel = boot_factory_product(
        resolved,
        workspace=workspace,
        contract_resolver=resolver,
    )
    application, factory = factory_application(
        artifact,
        kernel=kernel,
        workspace=workspace,
        lease_root=tmp_path / "leases",
    )
    blocked = await application.start_and_run(
        invocation_id="toy-b-1",
        entrypoint="review",
        graph_input={},
        execution_factory=factory,
    )
    assert blocked.status == "interrupted", blocked
    completed = await application.resume(
        invocation_id="toy-b-1",
        execution_factory=factory,
        resume="approve",
    )
    values = await invocation_values(artifact, "toy-b-1", "review")
    assert completed.status == "completed", completed
    assert values.get("left") is True
    assert values.get("child") is True
    assert values.get("combined") is True
    assert values.get("review") == "approve"
    assert (project_root / "left.txt").read_bytes() == b"left\n"
    assert (project_root / "child.txt").read_bytes() == b"child\n"


async def test_toy_b_replay_is_deterministic_and_products_are_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toy_b = _toy_composition(tmp_path / "toy-b", monkeypatch, "b")
    first = await _run_to_completion(tmp_path / "first", toy_b, invocation_id="toy-b-replay")
    second = await _run_to_completion(tmp_path / "second", toy_b, invocation_id="toy-b-replay")
    assert second[0] == first[0]
    assert second[1] == first[1]
    toy_a = _toy_composition(tmp_path / "toy-a", monkeypatch, "a")
    assert toy_a.manifest.product_id == "toy.a"
    assert toy_b.manifest.product_id == "toy.b"
    assert toy_a.lock_digest != toy_b.lock_digest
    assert not hasattr(toy_a, "workflow")
    assert not hasattr(toy_b, "workflow")
