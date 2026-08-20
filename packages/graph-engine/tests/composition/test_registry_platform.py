from __future__ import annotations

import base64
import csv
from dataclasses import replace
import hashlib
from importlib import metadata
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest
import yaml

from graph_engine import ENGINE_API_VERSION
from graph_engine.composition import (
    ConfigTreePluginSource,
    FrozenComposition,
    InvocationLock,
    LockedProduct,
    PluginRequirement,
    ProductFileSource,
    ProductManifest,
    RegistryPlatform,
    ResolutionRequest,
    SourceKey,
    SourceRole,
    WheelPluginSource,
    WheelProductSource,
)
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.lock import _locked_source
from graph_engine.composition.registries import RegistryConflict
from graph_engine.composition.resolver import _capture_editable_engine_snapshot
from graph_engine.composition.sources import _snapshot_installed_engine_distribution
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.graph.compiler import CompileError
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)


class _Handler:
    async def execute(self, _request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ok": True})


class _EffectHandler:
    async def apply(self, _intent: EffectIntent, _key: str) -> EffectApplyResult:
        return EffectApplyResult.applied({"ok": True})

    async def reconcile(self, _intent: EffectIntent, _key: str) -> EffectReconcileResult:
        return EffectReconcileResult.applied({"ok": True})


class _PluginProvider:
    def __init__(
        self,
        plugin_id: str,
        *,
        dependencies: tuple[tuple[str, str], ...] = (),
        contribution: PluginContribution | None = None,
        descriptors: tuple[PluginDescriptor, ...] | None = None,
    ) -> None:
        self._descriptors = descriptors or (
            PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id=plugin_id,
                plugin_version="1.0.0",
                engine_api=ENGINE_API_VERSION,
                dependencies=tuple(
                    PluginDependency(dependency_id, specifier) for dependency_id, specifier in dependencies
                ),
                task_handlers=(f"{plugin_id}.greet",),
                commit_validators=(),
            ),
        )
        self._descriptor_calls = 0
        self._contribution = contribution or PluginContribution(
            task_handlers={f"{plugin_id}.greet": _Handler()}
        )

    def descriptor(self) -> PluginDescriptor:
        index = min(self._descriptor_calls, len(self._descriptors) - 1)
        self._descriptor_calls += 1
        return self._descriptors[index]

    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        return self._contribution


class _ProductProvider:
    def __init__(self, manifest: ProductManifest) -> None:
        self._manifest = manifest

    def manifest(self) -> ProductManifest:
        return self._manifest


class _DriftingProductProvider(_ProductProvider):
    def __init__(
        self,
        stable: ProductManifest,
        drifted: ProductManifest,
    ) -> None:
        super().__init__(stable)
        self._manifests = (stable, drifted, drifted)
        self._manifest_calls = 0
        self.drift_enabled = False

    def manifest(self) -> ProductManifest:
        if not self.drift_enabled:
            return super().manifest()
        index = min(self._manifest_calls, len(self._manifests) - 1)
        self._manifest_calls += 1
        return self._manifests[index]


class _MetadataProvider:
    def __init__(self, distributions: dict[str, metadata.Distribution]) -> None:
        self._distributions = distributions

    def distribution(self, name: str) -> metadata.Distribution:
        try:
            return self._distributions[name]
        except KeyError as error:
            raise metadata.PackageNotFoundError(name) from error


def _workflow(
    capability: str = "toy.runtime.greet",
    *,
    schemas: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
    effects: tuple[str, ...] = (),
    entrypoints: dict[str, str] | None = None,
) -> WorkflowDef:
    return WorkflowDef.model_validate(
        {
            "name": "toy",
            "entrypoints": entrypoints or {"hello": "root"},
            "schemas": schemas,
            "resources": resources,
            "effects": effects,
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 1}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "greet",
                    "nodes": {
                        "greet": {
                            "kind": "task",
                            "capability": capability,
                            "retry": "once",
                            "timeout": "short",
                        },
                        "done": {"kind": "end"},
                    },
                    "edges": [{"from": "greet", "to": "done"}],
                }
            },
        }
    )


def _manifest(
    *,
    product_id: str = "toy.a",
    plugins: tuple[PluginRequirement, ...] | None = None,
    workflow: WorkflowDef | None = None,
    configuration: dict[str, dict[str, object]] | None = None,
) -> ProductManifest:
    selected_workflow = workflow or _workflow()
    return ProductManifest(
        schema_version="1",
        source=None,
        product_id=product_id,
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=plugins or (PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),),
        entrypoints=dict(selected_workflow.entrypoints),
        configuration=configuration or {},
        workflow=selected_workflow,
    )


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _manifest_declaration(manifest: ProductManifest) -> dict[str, object]:
    document = manifest.model_dump(mode="json")
    if manifest.workflow is not None:
        document["workflow"] = manifest.workflow.model_dump(mode="json", exclude_defaults=True)
    return document


def _distribution(
    root: Path,
    *,
    distribution_name: str,
    entrypoint_group: str,
    entrypoint_name: str,
    declaration_path: str | None = None,
    declaration: dict[str, object] | None = None,
) -> tuple[metadata.Distribution, Path]:
    package_name = distribution_name.replace("-", "_")
    site = root / distribution_name
    package = site / package_name
    package.mkdir(parents=True)
    module_path = package / "__init__.py"
    module_path.write_text("provider = object()\n", encoding="utf-8")
    declaration_file: Path | None = None
    if declaration_path is not None and declaration is not None:
        declaration_file = site / declaration_path
        declaration_file.parent.mkdir(parents=True, exist_ok=True)
        declaration_file.write_bytes(canonical_json_bytes(declaration))
    dist_info = site / f"{package_name}-1.0.0.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    entrypoints_path = dist_info / "entry_points.txt"
    entrypoints_path.write_text(
        f"[{entrypoint_group}]\n{entrypoint_name} = {package_name}:provider\n",
        encoding="utf-8",
    )
    record_path = dist_info / "RECORD"
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        declared_files = (module_path, metadata_path, entrypoints_path)
        if declaration_file is not None:
            declared_files = (module_path, declaration_file, metadata_path, entrypoints_path)
        for path in declared_files:
            relative = path.relative_to(site).as_posix()
            content = path.read_bytes()
            writer.writerow((relative, _record_hash(content), len(content)))
        writer.writerow((record_path.relative_to(site).as_posix(), "", ""))
    return metadata.Distribution.at(dist_info), module_path


def _combined_distribution(
    root: Path,
    *,
    product_id: str,
    plugin_id: str,
    product_manifest: ProductManifest,
    plugin_descriptor: PluginDescriptor,
) -> tuple[metadata.Distribution, dict[str, Path]]:
    site = root / "toy-combined"
    package = site / "toy_combined"
    package.mkdir(parents=True)
    paths = {
        "toy_combined": package / "__init__.py",
        "toy_combined.product": package / "product.py",
        "toy_combined.plugin": package / "plugin.py",
    }
    for path in paths.values():
        path.write_text("provider = object()\n", encoding="utf-8")
    product_declaration = package / "product-declaration.json"
    product_declaration.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "product",
                "source": product_manifest.source.model_dump(mode="json"),
                "manifest": _manifest_declaration(product_manifest),
            }
        )
    )
    plugin_declaration = package / "plugin-declaration.json"
    plugin_declaration.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "plugin",
                "source": plugin_descriptor.source.model_dump(mode="json"),
                "descriptor": plugin_descriptor.model_dump(mode="json"),
            }
        )
    )
    dist_info = site / "toy_combined-1.0.0.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_text(
        "Metadata-Version: 2.1\nName: toy-combined\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    entrypoints_path = dist_info / "entry_points.txt"
    entrypoints_path.write_text(
        "[graph_engine.products]\n"
        f"{product_id} = toy_combined.product:provider\n"
        "[graph_engine.plugins]\n"
        f"{plugin_id} = toy_combined.plugin:provider\n",
        encoding="utf-8",
    )
    record_path = dist_info / "RECORD"
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        for path in (
            *paths.values(),
            product_declaration,
            plugin_declaration,
            metadata_path,
            entrypoints_path,
        ):
            relative = path.relative_to(site).as_posix()
            content = path.read_bytes()
            writer.writerow((relative, _record_hash(content), len(content)))
        writer.writerow((record_path.relative_to(site).as_posix(), "", ""))
    return metadata.Distribution.at(dist_info), paths


def _platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    product: _ProductProvider | None = None,
    plugins: dict[str, _PluginProvider] | None = None,
    load_log: list[tuple[str, str]] | None = None,
) -> tuple[RegistryPlatform, dict[str, WheelPluginSource], WheelProductSource | None]:
    distributions: dict[str, metadata.Distribution] = {}
    load_values: dict[tuple[str, str], tuple[object, Path]] = {}
    product_source: WheelProductSource | None = None
    if product is not None:
        initial_manifest = product.manifest()
        declaration_path = "toy_product/product-declaration.json"
        source_expectation = ProviderSource(
            distribution="toy-product",
            version="1.0.0",
            entrypoint_group="graph_engine.products",
            entrypoint_name=initial_manifest.product_id,
            entrypoint_value="toy_product:provider",
            declaration_path=declaration_path,
        )
        product._manifest = initial_manifest.model_copy(update={"source": source_expectation})
        if isinstance(product, _DriftingProductProvider):
            product._manifests = tuple(
                manifest.model_copy(update={"source": source_expectation}) for manifest in product._manifests
            )
            product._manifest = product._manifests[0]
        static_manifest = product._manifest
        product_distribution, module_path = _distribution(
            tmp_path,
            distribution_name="toy-product",
            entrypoint_group="graph_engine.products",
            entrypoint_name=static_manifest.product_id,
            declaration_path=declaration_path,
            declaration={
                "schema_version": "1",
                "kind": "product",
                "source": source_expectation.model_dump(mode="json"),
                "manifest": _manifest_declaration(static_manifest),
            },
        )
        distributions["toy-product"] = product_distribution
        load_values[("graph_engine.products", static_manifest.product_id)] = (product, module_path)
        product_source = WheelProductSource(
            distribution="toy-product",
            entrypoint_name=static_manifest.product_id,
            declaration_path=declaration_path,
        )
    plugin_sources: dict[str, WheelPluginSource] = {}
    for plugin_id, provider in (plugins or {}).items():
        distribution_name = plugin_id.replace(".", "-")
        declaration_path = f"{distribution_name.replace('-', '_')}/plugin-declaration.json"
        source_expectation = ProviderSource(
            distribution=distribution_name,
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=f"{distribution_name.replace('-', '_')}:provider",
            declaration_path=declaration_path,
        )
        provider._descriptors = tuple(
            descriptor.model_copy(update={"source": source_expectation})
            for descriptor in provider._descriptors
        )
        static_descriptor = provider._descriptors[0]
        distribution, module_path = _distribution(
            tmp_path,
            distribution_name=distribution_name,
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            declaration_path=declaration_path,
            declaration={
                "schema_version": "1",
                "kind": "plugin",
                "source": source_expectation.model_dump(mode="json"),
                "descriptor": static_descriptor.model_dump(mode="json"),
            },
        )
        distributions[distribution_name] = distribution
        load_values[("graph_engine.plugins", plugin_id)] = (provider, module_path)
        plugin_sources[plugin_id] = WheelPluginSource(
            distribution=distribution_name,
            entrypoint_name=plugin_id,
            declaration_path=declaration_path,
        )

    def load(entrypoint: metadata.EntryPoint) -> object:
        if load_log is not None:
            load_log.append((entrypoint.group, entrypoint.name))
        provider, module_path = load_values[(entrypoint.group, entrypoint.name)]
        module = ModuleType(entrypoint.module)
        module.__file__ = str(module_path)
        monkeypatch.setitem(sys.modules, entrypoint.module, module)
        monkeypatch.setattr(provider, "__module__", entrypoint.module, raising=False)
        return provider

    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    return (
        RegistryPlatform(metadata_provider=_MetadataProvider(distributions)),
        plugin_sources,
        product_source,
    )


def test_invalid_topology_executes_no_wheel_provider_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
        dependencies=(PluginDependency("toy.flow", "==1.0.0"),),
    )
    flow_descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.flow",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.flow.greet",),
        commit_validators=(),
        dependencies=(PluginDependency("toy.runtime", "==1.0.0"),),
    )
    load_log: list[tuple[str, str]] = []
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(
            _manifest(
                plugins=(
                    PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),
                    PluginRequirement(plugin_id="toy.flow", version_specifier="==1.0.0"),
                )
            )
        ),
        plugins={
            "toy.runtime": _PluginProvider("toy.runtime", descriptors=(runtime_descriptor,)),
            "toy.flow": _PluginProvider("toy.flow", descriptors=(flow_descriptor,)),
        },
        load_log=load_log,
    )
    assert product_source is not None

    with pytest.raises(DependencyConflict, match="cycle"):
        platform.resolve(
            ResolutionRequest(
                product=product_source,
                plugins=(plugins["toy.runtime"], plugins["toy.flow"]),
            )
        )

    assert load_log == []


def test_registry_platform_resolves_one_frozen_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _PluginProvider("toy.runtime")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    assert isinstance(composition, FrozenComposition)
    assert composition.manifest.product_id == "toy.a"
    assert composition.workflow.entrypoints == {"hello": "root"}
    assert composition.lock.digest == composition.lock_digest
    assert composition.registries.capabilities.task_handlers["toy.runtime.greet"]
    assert composition.providers["toy.runtime"] is provider
    assert tuple(composition.registries.sources.entries) == (
        SourceKey(SourceRole.ENGINE, "graph.engine"),
        SourceKey(SourceRole.PLUGIN, "toy.runtime"),
        SourceKey(SourceRole.PRODUCT, "toy.a"),
    )
    assert thaw_json(composition.lock.configuration) == {}
    assert composition.lock.configuration_digest == canonical_digest({})
    compiled_projection = cast(dict[str, object], thaw_json(composition.lock.compiled_workflow))
    assert compiled_projection["entrypoints"] == {"hello": "root"}
    assert canonical_digest(compiled_projection) == composition.lock.compiled_workflow_digest
    assert thaw_json(composition.lock.registry_projections.sources)
    assert thaw_json(composition.lock.registry_projections.capabilities)
    assert thaw_json(composition.lock.capability_bindings) == []
    assert composition.lock.capability_bindings_digest == canonical_digest([])

    repeated = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))
    assert repeated == composition
    assert repeated.lock.canonical_bytes == composition.lock.canonical_bytes

    with pytest.raises(TypeError):
        composition.providers["toy.other"] = object()  # type: ignore[index]
    with pytest.raises(ValueError, match="composition digest"):
        replace(composition, digest="0" * 64)
    drifted_descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=1,<2",
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
    )
    with pytest.raises(ValueError, match="descriptor"):
        replace(composition, descriptors=(drifted_descriptor,))

    engine_identity = cast(dict[str, object], thaw_json(composition.lock.engine.identity))
    drifted_engine = composition.lock.engine.model_copy(
        update={"identity": {**engine_identity, "version": "9.9.9"}}
    )
    drifted_lock = InvocationLock.create(
        engine_api=composition.lock.engine_api,
        engine=drifted_engine,
        engine_digest=drifted_engine.digest,
        product=composition.lock.product,
        plugins=composition.lock.plugins,
        dependency_order=composition.lock.dependency_order,
        registry_projections=composition.lock.registry_projections,
        registry_digests=composition.lock.registry_digests,
        configuration=composition.lock.configuration,
        configuration_digest=composition.lock.configuration_digest,
        capability_bindings=composition.lock.capability_bindings,
        capability_bindings_digest=composition.lock.capability_bindings_digest,
        compiled_workflow=composition.lock.compiled_workflow,
        compiled_workflow_digest=composition.lock.compiled_workflow_digest,
    )
    with pytest.raises(ValueError, match="engine source"):
        replace(
            composition,
            lock=drifted_lock,
            digest=canonical_digest({"lock_digest": drifted_lock.digest}),
        )

    forged_manifest_document = composition.lock.product.model_dump(mode="json")["manifest"]
    assert isinstance(forged_manifest_document, dict)
    forged_manifest_document["plugins"] = [{"plugin_id": "toy.runtime", "version_specifier": "==9.9.9"}]
    forged_manifest = composition.manifest.model_copy(
        update={"plugins": (PluginRequirement(plugin_id="toy.runtime", version_specifier="==9.9.9"),)}
    )
    forged_product = LockedProduct(
        product_id=composition.lock.product.product_id,
        product_version=composition.lock.product.product_version,
        manifest=forged_manifest_document,
        manifest_digest=canonical_digest(forged_manifest_document),
        source=composition.lock.product.source,
    )
    with pytest.raises(ValueError, match="dependency declarations"):
        InvocationLock.create(
            engine_api=composition.lock.engine_api,
            engine=composition.lock.engine,
            engine_digest=composition.lock.engine_digest,
            product=forged_product,
            plugins=composition.lock.plugins,
            dependency_order=composition.lock.dependency_order,
            registry_projections=composition.lock.registry_projections,
            registry_digests=composition.lock.registry_digests,
            configuration=composition.lock.configuration,
            configuration_digest=composition.lock.configuration_digest,
            capability_bindings=composition.lock.capability_bindings,
            capability_bindings_digest=composition.lock.capability_bindings_digest,
            compiled_workflow=composition.lock.compiled_workflow,
            compiled_workflow_digest=composition.lock.compiled_workflow_digest,
        )

    forged_lock = composition.lock.model_copy()
    object.__setattr__(forged_lock, "product", forged_product)
    with pytest.raises(ValueError, match="dependency declarations"):
        replace(
            composition,
            manifest=forged_manifest,
            lock=forged_lock,
            digest=canonical_digest({"lock_digest": forged_lock.digest}),
        )


def test_registry_platform_strictly_parses_mapping_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None
    raw_request = {
        "product": product_source.model_dump(mode="python"),
        "plugins": [plugins["toy.runtime"].model_dump(mode="python")],
    }

    composition = platform.resolve(cast(ResolutionRequest, raw_request))

    assert composition.manifest.product_id == "toy.a"


def test_product_and_plugin_may_share_their_domain_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_id = "toy.same"
    manifest = _manifest(
        product_id=plugin_id,
        plugins=(PluginRequirement(plugin_id=plugin_id, version_specifier="==1.0.0"),),
        workflow=_workflow(f"{plugin_id}.greet"),
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(manifest),
        plugins={plugin_id: _PluginProvider(plugin_id)},
    )
    assert product_source is not None

    composition = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],)))

    assert composition.manifest.product_id == plugin_id
    assert composition.providers[plugin_id]


def test_product_and_plugin_resolve_from_sibling_modules_in_one_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain_id = "toy.combined"
    product = _ProductProvider(
        _manifest(
            product_id=domain_id,
            plugins=(PluginRequirement(plugin_id=domain_id, version_specifier="==1.0.0"),),
            workflow=_workflow(f"{domain_id}.greet"),
        )
    )
    plugin = _PluginProvider(domain_id)
    product_path = "toy_combined/product-declaration.json"
    plugin_path = "toy_combined/plugin-declaration.json"
    product_source_expectation = ProviderSource(
        distribution="toy-combined",
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name=domain_id,
        entrypoint_value="toy_combined.product:provider",
        declaration_path=product_path,
    )
    plugin_source_expectation = ProviderSource(
        distribution="toy-combined",
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=domain_id,
        entrypoint_value="toy_combined.plugin:provider",
        declaration_path=plugin_path,
    )
    product._manifest = product._manifest.model_copy(update={"source": product_source_expectation})
    plugin._descriptors = tuple(
        descriptor.model_copy(update={"source": plugin_source_expectation})
        for descriptor in plugin._descriptors
    )
    distribution, paths = _combined_distribution(
        tmp_path,
        product_id=domain_id,
        plugin_id=domain_id,
        product_manifest=product._manifest,
        plugin_descriptor=plugin._descriptors[0],
    )
    providers = {
        ("graph_engine.products", domain_id): product,
        ("graph_engine.plugins", domain_id): plugin,
    }

    def load(entrypoint: metadata.EntryPoint) -> object:
        parent = ModuleType("toy_combined")
        parent.__file__ = str(paths["toy_combined"])
        monkeypatch.setitem(sys.modules, "toy_combined", parent)
        module = ModuleType(entrypoint.module)
        module.__file__ = str(paths[entrypoint.module])
        monkeypatch.setitem(sys.modules, entrypoint.module, module)
        provider = providers[(entrypoint.group, entrypoint.name)]
        monkeypatch.setattr(provider, "__module__", entrypoint.module, raising=False)
        return provider

    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    platform = RegistryPlatform(metadata_provider=_MetadataProvider({"toy-combined": distribution}))

    composition = platform.resolve(
        ResolutionRequest(
            product=WheelProductSource(
                distribution="toy-combined",
                entrypoint_name=domain_id,
                declaration_path=product_path,
            ),
            plugins=(
                WheelPluginSource(
                    distribution="toy-combined",
                    entrypoint_name=domain_id,
                    declaration_path=plugin_path,
                ),
            ),
        )
    )

    assert composition.product_provider is product
    assert composition.providers[domain_id] is plugin


def _write_mixed_product(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "product_id": "toy.mixed",
                "product_version": "1.0.0",
                "engine_api": ENGINE_API_VERSION,
                "plugins": [
                    {"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"},
                    {"plugin_id": "toy.flow", "version_specifier": "==1.0.0"},
                ],
                "entrypoints": {"hello": "root"},
                "configuration": {"toy.runtime": {"greeting": "你好"}},
                "workflow": _workflow("toy.flow.greet").model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                ),
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def _write_flow_plugin(path: Path) -> None:
    path.mkdir()
    (path / "role.txt").write_text("friendly\n", encoding="utf-8")
    (path / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "plugin_id": "toy.flow",
                "plugin_version": "1.0.0",
                "engine_api": ENGINE_API_VERSION,
                "dependencies": [{"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"}],
                "files": [
                    {
                        "kind": "resource",
                        "resource_id": "toy.flow.role",
                        "path": "role.txt",
                        "media_type": "text/plain",
                    }
                ],
                "bindings": [
                    {
                        "capability_id": "toy.flow.greet",
                        "target_capability_id": "toy.runtime.greet",
                        "data": {"locale": "zh-CN"},
                        "resource_ids": ["toy.flow.role"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_resolution_is_identical_for_permuted_explicit_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_file = tmp_path / "product.yaml"
    flow_root = tmp_path / "flow"
    _write_mixed_product(product_file)
    _write_flow_plugin(flow_root)
    platform, plugin_sources, _product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    runtime = plugin_sources["toy.runtime"]
    flow = ConfigTreePluginSource(path=flow_root)
    product = ProductFileSource(path=product_file)

    first = platform.resolve(ResolutionRequest(product=product, plugins=(flow, runtime)))
    second = platform.resolve(ResolutionRequest(product=product, plugins=(runtime, flow)))

    assert first.lock.canonical_bytes == second.lock.canonical_bytes
    assert first.digest == second.digest
    assert first == second
    assert first.declarative_sources["toy.flow"].files == second.declarative_sources["toy.flow"].files


@pytest.mark.parametrize("case", ("missing", "extra"))
def test_resolution_rejects_nonexact_source_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    required = _PluginProvider("toy.runtime")
    extra = _PluginProvider("toy.extra")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": required, "toy.extra": extra},
    )
    assert product_source is not None
    selected = () if case == "missing" else (plugins["toy.runtime"], plugins["toy.extra"])

    with pytest.raises(DependencyConflict, match="missing|unexpected"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=selected))
    if case == "extra":
        assert extra._descriptor_calls == 0


def test_resolution_rejects_product_constraint_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        plugins=(
            PluginRequirement(
                plugin_id="toy.runtime",
                version_specifier=">=2",
            ),
        )
    )
    provider = _PluginProvider("toy.runtime")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(manifest),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None

    with pytest.raises(DependencyConflict, match=r"toy\.runtime==1\.0\.0"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))
    assert provider._descriptor_calls == 0


def test_missing_root_source_rejects_before_loading_other_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        plugins=(
            PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),
            PluginRequirement(plugin_id="toy.missing", version_specifier="==1.0.0"),
        )
    )
    provider = _PluginProvider("toy.runtime")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(manifest),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None

    with pytest.raises(DependencyConflict, match="missing"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))
    assert provider._descriptor_calls == 0


def test_resolution_rejects_drifted_provider_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
    )
    drifted = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=1,<2",
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
    )
    provider = _PluginProvider("toy.runtime", descriptors=(expected, drifted, drifted))
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    authenticated = provider._descriptors[0]

    with pytest.raises(Exception, match="descriptor|declarations"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))

    provider._descriptors = (authenticated,)
    provider._descriptor_calls = 0
    corrected = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))
    assert corrected.providers["toy.runtime"] is provider


def test_source_roles_disjoin_adversarial_product_and_plugin_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "toy.a.product-source"
    workflow = _workflow(f"{plugin_id}.greet")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(
            _manifest(
                product_id="toy.a",
                plugins=(PluginRequirement(plugin_id=plugin_id, version_specifier="==1.0.0"),),
                workflow=workflow,
            )
        ),
        plugins={plugin_id: _PluginProvider(plugin_id)},
    )
    assert product_source is not None

    composition = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],)))

    assert SourceKey(SourceRole.PRODUCT, "toy.a") in composition.registries.sources.entries
    assert SourceKey(SourceRole.PLUGIN, plugin_id) in composition.registries.sources.entries


def test_resolution_rejects_drifted_product_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _manifest()
    drifted = expected.model_copy(update={"engine_api": ">=1,<2"})
    product = _DriftingProductProvider(expected, drifted)
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=product,
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None
    product.drift_enabled = True

    with pytest.raises(Exception, match="manifest|declarations"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))


@pytest.mark.parametrize(
    ("reference_kind", "expected"),
    (
        ("capability", "unknown capability"),
        ("schema", "unknown schema"),
        ("resource", "unknown resource"),
        ("effect", "unknown effect"),
    ),
)
def test_resolution_rejects_unknown_graph_registry_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
    expected: str,
) -> None:
    kwargs: dict[str, object] = {}
    capability = "toy.runtime.greet"
    if reference_kind == "capability":
        capability = "toy.runtime.missing"
    else:
        kwargs[f"{reference_kind}s"] = (f"toy.runtime.missing-{reference_kind}",)
    workflow = _workflow(capability, **kwargs)  # type: ignore[arg-type]
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=workflow)),
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None

    with pytest.raises(CompileError, match=expected):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))


def test_resolution_rejects_unknown_effect_schema_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contribution = PluginContribution(
        effects=(
            EffectRegistration(
                kind="toy.runtime.audit",
                intent_schema_id="toy.runtime.missing-intent",
                receipt_schema_id="toy.runtime.missing-receipt",
                handler=_EffectHandler(),
                policy=EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
            ),
        )
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(),
        commit_validators=(),
        effects=("toy.runtime.audit",),
    )
    provider = _PluginProvider("toy.runtime", contribution=contribution, descriptors=(descriptor,))
    workflow = WorkflowDef.model_validate(
        {
            "name": "toy",
            "entrypoints": {"hello": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "done",
                    "nodes": {"done": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=workflow)),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None

    with pytest.raises(RegistryConflict, match="unknown effect intent schema"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))


def test_resolution_rejects_invalid_product_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(configuration={"toy.unknown": {"enabled": True}})),
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None

    with pytest.raises(Exception, match="configuration"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))


def test_product_manifest_requires_one_workflow_form_and_entrypoint_closure() -> None:
    workflow = _workflow()
    base = {
        "schema_version": "1",
        "source": None,
        "product_id": "toy.a",
        "product_version": "1.0.0",
        "engine_api": ENGINE_API_VERSION,
        "plugins": [{"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"}],
        "entrypoints": {"hello": "root"},
        "configuration": {},
    }
    with pytest.raises(ValueError, match="exactly one workflow form"):
        ProductManifest.model_validate({**base, "workflow": workflow, "workflow_resource_id": "toy.a.flow"})
    with pytest.raises(ValueError, match="entrypoints"):
        ProductManifest.model_validate({**base, "entrypoints": {"other": "root"}, "workflow": workflow})


def test_resource_workflow_must_close_product_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_file = tmp_path / "product.yaml"
    flow_root = tmp_path / "flow"
    _write_flow_plugin(flow_root)
    workflow_path = flow_root / "workflow.yaml"
    workflow_path.write_text(
        yaml.safe_dump(
            _workflow("toy.flow.greet", entrypoints={"other": "root"}).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
        ),
        encoding="utf-8",
    )
    plugin_document = yaml.safe_load((flow_root / "plugin.yaml").read_text(encoding="utf-8"))
    plugin_document["files"].append(
        {
            "kind": "resource",
            "resource_id": "toy.flow.workflow",
            "path": "workflow.yaml",
            "media_type": "application/vnd.graph-engine.workflow+yaml",
        }
    )
    (flow_root / "plugin.yaml").write_text(yaml.safe_dump(plugin_document, sort_keys=False), encoding="utf-8")
    product_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "product_id": "toy.mixed",
                "product_version": "1.0.0",
                "engine_api": ENGINE_API_VERSION,
                "plugins": [
                    {"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"},
                    {"plugin_id": "toy.flow", "version_specifier": "==1.0.0"},
                ],
                "entrypoints": {"hello": "root"},
                "configuration": {},
                "workflow_resource_id": "toy.flow.workflow",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    platform, plugins, _ = _platform(
        tmp_path / "wheels",
        monkeypatch,
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )

    with pytest.raises(Exception, match="entrypoints"):
        platform.resolve(
            ResolutionRequest(
                product=ProductFileSource(path=product_file),
                plugins=(plugins["toy.runtime"], ConfigTreePluginSource(path=flow_root)),
            )
        )


def test_failed_resolution_has_no_runtime_write_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    platform, _plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=_ProductProvider(_manifest()),
    )
    assert product_source is not None

    with pytest.raises(DependencyConflict):
        platform.resolve(ResolutionRequest(product=product_source, plugins=()))
    assert not runtime_root.exists()


def test_registry_digests_cover_complete_registered_schema_resource_and_effect_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas = (
        SchemaContribution("toy.runtime.intent", "application/schema+json", b'{"type":"object"}'),
        SchemaContribution("toy.runtime.receipt", "application/schema+json", b'{"type":"object"}'),
    )
    contribution = PluginContribution(
        schemas=schemas,
        effects=(
            EffectRegistration(
                kind="toy.runtime.audit",
                intent_schema_id="toy.runtime.intent",
                receipt_schema_id="toy.runtime.receipt",
                handler=_EffectHandler(),
                policy=EffectPolicy(max_attempts=2, timeout_seconds=1.5, backoff_seconds=0.25),
            ),
        ),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(),
        commit_validators=(),
        schemas=("toy.runtime.intent", "toy.runtime.receipt"),
        effects=("toy.runtime.audit",),
    )
    end_workflow = WorkflowDef.model_validate(
        {
            "name": "toy",
            "entrypoints": {"hello": "root"},
            "schemas": ["toy.runtime.intent", "toy.runtime.receipt"],
            "effects": ["toy.runtime.audit"],
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "done",
                    "nodes": {"done": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=end_workflow)),
        plugins={
            "toy.runtime": _PluginProvider(
                "toy.runtime", contribution=contribution, descriptors=(descriptor,)
            )
        },
    )
    assert product_source is not None

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    assert composition.lock.registry_digests.schemas != "0" * 64
    assert composition.lock.registry_digests.effects != "0" * 64
    schema_projection = cast(
        list[dict[str, object]], thaw_json(composition.lock.registry_projections.schemas)
    )
    effect_projection = cast(
        list[dict[str, object]], thaw_json(composition.lock.registry_projections.effects)
    )
    assert {item["schema_id"] for item in schema_projection} == {
        "toy.runtime.intent",
        "toy.runtime.receipt",
    }
    assert effect_projection == [
        {
            "implementation_digest": composition.registries.sources.entries[
                SourceKey(SourceRole.PLUGIN, "toy.runtime")
            ].snapshot.digest,
            "intent_schema_id": "toy.runtime.intent",
            "kind": "toy.runtime.audit",
            "owner_id": "toy.runtime",
            "policy": {
                "backoff_seconds": 0.25,
                "max_attempts": 2,
                "timeout_seconds": 1.5,
            },
            "receipt_schema_id": "toy.runtime.receipt",
        }
    ]
    assert cast(object, composition.registries.effects.entries["toy.runtime.audit"].handler) is not None


def test_editable_engine_snapshot_includes_packaging_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "graph-engine"
    package_root = project_root / "graph_engine"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\nname = "graph-engine"\nversion = "1.0.0"\n', encoding="utf-8")

    first = _capture_editable_engine_snapshot(project_root)
    pyproject.write_text('[project]\nname = "graph-engine"\nversion = "1.0.1"\n', encoding="utf-8")
    second = _capture_editable_engine_snapshot(project_root)

    assert tuple(item.path for item in first.files) == (
        "graph_engine/__init__.py",
        "pyproject.toml",
    )
    assert first.identity.distribution == "graph-engine"
    assert first.identity.version == "1.0.0"
    assert first.identity.engine_installation == "editable"
    assert first.identity.root == project_root.resolve()
    assert first.digest != second.digest


def test_installed_engine_snapshot_uses_authenticated_wheel_metadata(tmp_path: Path) -> None:
    distribution, _module_path = _distribution(
        tmp_path,
        distribution_name="graph-engine",
        entrypoint_group="console_scripts",
        entrypoint_name="graph-engine",
    )

    snapshot = _snapshot_installed_engine_distribution(distribution)

    assert snapshot.identity.kind.value == "engine"
    assert snapshot.identity.distribution == "graph-engine"
    assert snapshot.identity.version == "1.0.0"
    assert snapshot.identity.engine_installation == "installed"
    assert any(item.path.endswith(".dist-info/METADATA") for item in snapshot.files)
    assert any(item.path.endswith(".dist-info/RECORD") for item in snapshot.files)


def test_installed_engine_lock_identity_is_relocatable(tmp_path: Path) -> None:
    first_distribution, _ = _distribution(
        tmp_path / "one",
        distribution_name="graph-engine",
        entrypoint_group="console_scripts",
        entrypoint_name="graph-engine",
    )
    second_distribution, _ = _distribution(
        tmp_path / "two",
        distribution_name="graph-engine",
        entrypoint_group="console_scripts",
        entrypoint_name="graph-engine",
    )

    first = _snapshot_installed_engine_distribution(first_distribution)
    second = _snapshot_installed_engine_distribution(second_distribution)

    assert first.identity.root != second.identity.root
    assert first.digest == second.digest
    assert _locked_source(first) == _locked_source(second)
    assert "root" not in thaw_json(_locked_source(first).identity)
