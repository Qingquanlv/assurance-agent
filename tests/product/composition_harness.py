from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import importlib
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal, cast
import zipfile

from graph_engine.graph.compiler import CompiledWorkflow

import pytest

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.composition import (
    CapabilityBindingEntry,
    ConfigTreePluginSource,
    FrozenComposition,
    WheelPluginSource,
)
from graph_engine.frozen_json import thaw_json

from tests.product.conformance import ALL_BINDING_IDS, EVIDENCE_ROOT

SHADOW_VALIDATOR_CLONE_ID = "test.assurance.execution.validator-parity.v1"

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
_DEPLOYMENT_FIXTURES = _FIXTURE_ROOT / "deployment"
_CONFIG_FIXTURE = _FIXTURE_ROOT / "project-config"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_WHEELS: tuple[tuple[str, str], ...] = (
    ("assurance-product", "packages/products/assurance-product"),
    ("assurance-intake", "packages/capabilities/assurance-intake"),
    ("assurance-generation", "packages/capabilities/assurance-generation"),
    ("assurance-execution", "packages/capabilities/assurance-execution"),
    ("assurance-healing", "packages/capabilities/assurance-healing"),
    ("assurance-quality", "packages/capabilities/assurance-quality"),
    ("assurance-improvement", "packages/capabilities/assurance-improvement"),
    ("agent-runtime-opencode", "packages/adapters/agent-runtime-opencode"),
    ("agent-runtime-cursor", "packages/adapters/agent-runtime-cursor"),
)
COVERAGE_PATH = EVIDENCE_ROOT / "binding-coverage.json"


@dataclass(frozen=True)
class InstalledSources:
    deployments: dict[str, WheelPluginSource]
    configuration_tree: ConfigTreePluginSource
    wheels: dict[str, Path]
    extract_roots: dict[str, Path]


@dataclass(frozen=True)
class CompiledProduct:
    workflow: CompiledWorkflow
    composition: FrozenComposition


def evict_generated_binding_modules() -> None:
    for name in tuple(sys.modules):
        if name == "assurance_product_bindings" or name.startswith("assurance_product_bindings_"):
            sys.modules.pop(name, None)


def request_for(adapter: str, installed_sources: InstalledSources):
    from assurance_product.product import AssuranceCompositionRequest

    if adapter == "opencode":
        entrypoint: Literal["assurance-opencode", "assurance-cursor"] = "assurance-opencode"
    elif adapter == "cursor":
        entrypoint = "assurance-cursor"
    else:
        raise ValueError(f"unsupported adapter: {adapter!r}")
    return AssuranceCompositionRequest(
        product_entrypoint=entrypoint,
        deployment_source=installed_sources.deployments[adapter],
        configuration_tree=installed_sources.configuration_tree,
    )


def build_installed_sources(root: Path) -> InstalledSources:
    wheels: dict[str, Path] = {}
    deployments: dict[str, WheelPluginSource] = {}
    extract_roots: dict[str, Path] = {}
    _extract_workspace_wheels(root, extract_roots)
    _import_extracted_workspace_packages()
    from assurance_product.binding_builder import build_deployment_wheel

    for adapter in ("opencode", "cursor"):
        built = build_deployment_wheel(
            _DEPLOYMENT_FIXTURES / f"{adapter}.yaml",
            root / f"build-{adapter}",
        )
        extract_root = _extract_wheel(built.wheel, root / f"extract-{adapter}")
        wheels[adapter] = built.wheel
        extract_roots[adapter] = extract_root
        deployments[adapter] = WheelPluginSource(
            distribution=built.distribution,
            entrypoint_name="deployment",
            declaration_path=built.declaration_path,
        )
    return InstalledSources(
        deployments=deployments,
        configuration_tree=ConfigTreePluginSource(path=_CONFIG_FIXTURE.resolve()),
        wheels=wheels,
        extract_roots=extract_roots,
    )


def copy_config_tree(destination: Path) -> ConfigTreePluginSource:
    shutil.copytree(_CONFIG_FIXTURE, destination)
    return ConfigTreePluginSource(path=destination.resolve())


def project_binding_coverage(composition: FrozenComposition) -> dict[str, dict[str, JSONValue]]:
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    if set(bindings) != set(ALL_BINDING_IDS):
        raise AssertionError("composition binding set is not the exact 99 aliases")
    projected: dict[str, dict[str, JSONValue]] = {}
    for binding_id in ALL_BINDING_IDS:
        entry = bindings[binding_id]
        projected[binding_id] = {
            "data": thaw_json(entry.data),
            "owner_id": entry.owner_id,
            "phase": binding_id.rsplit(".", 1)[-1],
            "resource_ids": list(entry.resource_ids),
            "secret_handles": list(entry.secret_handles),
            "target_capability_id": entry.target_capability_id,
        }
    return projected


def coverage_bytes(projection: dict[str, dict[str, JSONValue]]) -> bytes:
    return canonical_json_bytes(cast(JSONValue, projection)) + b"\n"


def _extract_workspace_wheels(root: Path, extract_roots: dict[str, Path]) -> None:
    for name, project in _WORKSPACE_WHEELS:
        output = root / f"workspace-build-{name}"
        output.mkdir()
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output), str(_REPO_ROOT / project)],
            check=True,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        wheel = next(output.glob("*.whl"))
        extract_roots[name] = _extract_wheel(wheel, root / f"workspace-extract-{name}")
    importlib.invalidate_caches()


def _evict_workspace_packages() -> None:
    prefixes = tuple(name.replace("-", "_") for name, _ in _WORKSPACE_WHEELS)
    for module_name in list(sys.modules):
        if module_name in prefixes or any(module_name.startswith(f"{prefix}.") for prefix in prefixes):
            del sys.modules[module_name]


def _import_extracted_workspace_packages() -> None:
    """Load extracted wheels with standard loaders, not pytest assertion rewriting."""
    _evict_workspace_packages()
    hooks = [finder for finder in sys.meta_path if type(finder).__name__ == "AssertionRewritingHook"]
    for hook in hooks:
        sys.meta_path.remove(hook)
    try:
        importlib.invalidate_caches()
        for distribution, _project in _WORKSPACE_WHEELS:
            importlib.import_module(distribution.replace("-", "_"))
    finally:
        for hook in reversed(hooks):
            if hook not in sys.meta_path:
                sys.meta_path.insert(0, hook)


def _extract_wheel(wheel: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    destination = destination.resolve()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(destination)
        assert any(name.endswith(".dist-info/RECORD") for name in archive.namelist())
    if str(destination) not in sys.path:
        sys.path.insert(0, str(destination))
    return destination


@pytest.fixture(scope="session")
def installed_sources(tmp_path_factory: pytest.TempPathFactory) -> Iterator[InstalledSources]:
    sources = build_installed_sources(tmp_path_factory.mktemp("assurance-composition"))
    try:
        yield sources
    finally:
        for extract_root in sources.extract_roots.values():
            extract = str(extract_root)
            while extract in sys.path:
                sys.path.remove(extract)


@pytest.fixture
def compiled_product_workflow(installed_sources: InstalledSources) -> CompiledWorkflow:
    from assurance_product.product import resolve_assurance_composition

    return resolve_assurance_composition(request_for("opencode", installed_sources)).workflow


@pytest.fixture
def compiled_for(installed_sources: InstalledSources):
    from assurance_product.product import resolve_assurance_composition

    def factory(adapter: str) -> CompiledProduct:
        composition = resolve_assurance_composition(request_for(adapter, installed_sources))
        return CompiledProduct(workflow=composition.workflow, composition=composition)

    return factory
