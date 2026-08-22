from __future__ import annotations

import base64
import csv
from dataclasses import replace
import hashlib
import importlib
from importlib import metadata
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest
import yaml

import graph_engine.composition.resolver as resolver_runtime
from graph_engine import ENGINE_API_VERSION
from graph_engine.composition import (
    CapabilityBindingEntry,
    CapabilityRegistry,
    CommitValidatorEntry,
    ConfigTreePluginSource,
    EffectRegistry,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    FrozenComposition,
    InvocationLock,
    LockedProduct,
    PluginRequirement,
    ProductFileSource,
    ProductManifest,
    RegistryPlatform,
    RegistrySet,
    ResourceEntry,
    ResourceRegistry,
    ResolutionError,
    ResolutionRequest,
    SourceFile,
    SourceKey,
    SourceRole,
    SourceSnapshot,
    SchemaEntry,
    SchemaRegistry,
    TaskHandlerEntry,
    WheelPluginSource,
    WheelProductSource,
)
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.lock import _locked_source, build_invocation_lock
from graph_engine.composition.models import ContributionAuthority, ExecutableAuthority
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import RegistryConflict
from graph_engine.composition.resolver import _capture_editable_engine_snapshot
from graph_engine.composition.sources import _snapshot_installed_engine_distribution
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.graph.compiler import CompileError
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    CandidateWriteSet,
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
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
    ValidationResult,
)
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.invocation_lock import InvocationDrift


class _Handler:
    async def execute(self, _request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ok": True})


class _EffectHandler:
    async def apply(self, _intent: EffectIntent, _key: str) -> EffectApplyResult:
        return EffectApplyResult.applied({"ok": True})

    async def reconcile(self, _intent: EffectIntent, _key: str) -> EffectReconcileResult:
        return EffectReconcileResult.applied({"ok": True})


class _Validator:
    def validate(
        self,
        _candidate: CandidateWriteSet,
        _context: ValidationContext,
    ) -> ValidationResult:
        return ValidationResult(accepted=True)


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
        self._contribution = contribution
        self._implementation_module: str | None = None

    def descriptor(self) -> PluginDescriptor:
        index = min(self._descriptor_calls, len(self._descriptors) - 1)
        self._descriptor_calls += 1
        return self._descriptors[index]

    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        if self._implementation_module is None:
            raise AssertionError("test provider lacks an authenticated implementation module")
        implementation = importlib.import_module(self._implementation_module)
        if self._contribution is not None:
            return PluginContribution(
                task_handlers={key: implementation.handler for key in self._contribution.task_handlers},
                commit_validators={
                    key: implementation.validator for key in self._contribution.commit_validators
                },
                schemas=self._contribution.schemas,
                resources=self._contribution.resources,
                effects=tuple(
                    replace(registration, handler=implementation.effect_handler)
                    for registration in self._contribution.effects
                ),
                bindings=self._contribution.bindings,
            )
        return PluginContribution(
            task_handlers={f"{self._descriptors[0].plugin_id}.greet": implementation.handler}
        )


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


class _LazyContributionProvider(_PluginProvider):
    def __init__(self, plugin_id: str, module_name: str) -> None:
        super().__init__(plugin_id)
        self.module_name = module_name
        self.fail = False

    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        helper = importlib.import_module(self.module_name)
        if self.fail:
            raise RuntimeError("contribution failed after lazy import")
        return PluginContribution(task_handlers={f"{self._descriptors[0].plugin_id}.greet": helper.handler})


class _LazyDescriptorProvider(_PluginProvider):
    def __init__(self, plugin_id: str, module_name: str) -> None:
        super().__init__(plugin_id)
        self.module_name = module_name
        self.lazy = False

    def descriptor(self) -> PluginDescriptor:
        if self.lazy:
            importlib.import_module(self.module_name)
        return super().descriptor()


class _LazyManifestProvider(_ProductProvider):
    def __init__(self, manifest: ProductManifest, module_name: str) -> None:
        super().__init__(manifest)
        self.module_name = module_name
        self.lazy = False

    def manifest(self) -> ProductManifest:
        if self.lazy:
            importlib.import_module(self.module_name)
        return super().manifest()


class _ImportedExecutableProvider(_PluginProvider):
    def __init__(
        self,
        descriptor: PluginDescriptor,
        module_name: str,
        executable_kind: str,
    ) -> None:
        super().__init__(descriptor.plugin_id, descriptors=(descriptor,))
        self.module_name = module_name
        self.executable_kind = executable_kind

    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        implementation = importlib.import_module(self.module_name)
        plugin_id = self._descriptors[0].plugin_id
        if self.executable_kind == "task_handler":
            return PluginContribution(task_handlers={f"{plugin_id}.greet": implementation.handler})
        if self.executable_kind == "commit_validator":
            return PluginContribution(commit_validators={f"{plugin_id}.validate": implementation.validator})
        return PluginContribution(
            schemas=(
                SchemaContribution(
                    f"{plugin_id}.intent",
                    "application/schema+json",
                    b'{"type":"object"}',
                ),
                SchemaContribution(
                    f"{plugin_id}.receipt",
                    "application/schema+json",
                    b'{"type":"object"}',
                ),
            ),
            effects=(
                EffectRegistration(
                    kind=f"{plugin_id}.audit",
                    intent_schema_id=f"{plugin_id}.intent",
                    receipt_schema_id=f"{plugin_id}.receipt",
                    handler=implementation.effect_handler,
                    policy=EffectPolicy(
                        max_attempts=1,
                        timeout_seconds=1,
                        backoff_seconds=0,
                    ),
                ),
            ),
        )


class _FreshInstanceProvider(_PluginProvider):
    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        assert self._implementation_module is not None
        implementation = importlib.import_module(self._implementation_module)
        descriptor = self._descriptors[0]
        return PluginContribution(
            task_handlers={registry_id: implementation.Handler() for registry_id in descriptor.task_handlers},
            commit_validators={
                registry_id: implementation.Validator() for registry_id in descriptor.commit_validators
            },
        )


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


def _passive_workflow() -> WorkflowDef:
    return WorkflowDef.model_validate(
        {
            "name": "passive",
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


def _add_distribution_file(
    distribution: metadata.Distribution,
    relative_path: str,
    content: bytes,
) -> Path:
    root = Path(distribution.locate_file(""))
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    record_path = next(root.glob("*.dist-info/RECORD"))
    with record_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    rows.insert(-1, [relative_path, _record_hash(content), str(len(content))])
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)
    return path


def _replace_distribution_file(
    distribution: metadata.Distribution,
    relative_path: str,
    content: bytes,
) -> None:
    root = Path(distribution.locate_file(""))
    (root / relative_path).write_bytes(content)
    record_path = next(root.glob("*.dist-info/RECORD"))
    with record_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    matches = [index for index, row in enumerate(rows) if row and row[0] == relative_path]
    assert len(matches) == 1
    rows[matches[0]] = [relative_path, _record_hash(content), str(len(content))]
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)


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
    module_path.write_text(
        "from graph_engine.plugin_api import (\n"
        "    EffectApplyResult, EffectReconcileResult, TaskOutcome, ValidationResult,\n"
        ")\n"
        "class Handler:\n"
        "    async def execute(self, _request, _context):\n"
        "        return TaskOutcome.succeeded({'ok': True})\n"
        "class Validator:\n"
        "    def validate(self, _candidate, _context):\n"
        "        return ValidationResult(accepted=True)\n"
        "class EffectHandler:\n"
        "    async def apply(self, _intent, _key):\n"
        "        return EffectApplyResult.applied({'ok': True})\n"
        "    async def reconcile(self, _intent, _key):\n"
        "        return EffectReconcileResult.applied({'ok': True})\n"
        "handler = Handler()\n"
        "validator = Validator()\n"
        "effect_handler = EffectHandler()\n"
        "provider = object()\n",
        encoding="utf-8",
    )
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
    paths["toy_combined.plugin"].write_text(
        "from graph_engine.plugin_api import TaskOutcome\n"
        "class Handler:\n"
        "    async def execute(self, _request, _context):\n"
        "        return TaskOutcome.succeeded({'ok': True})\n"
        "handler = Handler()\n"
        "provider = object()\n",
        encoding="utf-8",
    )
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
    module_roots = {"toy_product", *(plugin_id.replace(".", "_") for plugin_id in (plugins or {}))}
    for module_name in tuple(sys.modules):
        if any(module_name == root or module_name.startswith(f"{root}.") for root in module_roots):
            sys.modules.pop(module_name, None)
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
            import_roots=("",),
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
            import_roots=("",),
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
        provider._implementation_module = source_expectation.entrypoint_value.partition(":")[0]
        load_values[("graph_engine.plugins", plugin_id)] = (provider, module_path)
        plugin_sources[plugin_id] = WheelPluginSource(
            distribution=distribution_name,
            entrypoint_name=plugin_id,
            declaration_path=declaration_path,
        )

    def load(entrypoint: metadata.EntryPoint) -> object:
        if load_log is not None:
            load_log.append((entrypoint.group, entrypoint.name))
        provider, _module_path = load_values[(entrypoint.group, entrypoint.name)]
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
    assert composition.providers["toy.runtime"]._provider is provider
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


def test_public_frozen_composition_rejects_self_consistent_unowned_executable_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    helper_name = "toy_runtime.forged_helper"
    helper_content = (
        b"from graph_engine.plugin_api import TaskOutcome\n"
        b"class Handler:\n"
        b"    async def execute(self, _request, _context):\n"
        b"        return TaskOutcome.succeeded({'forged': True})\n"
        b"handler = Handler()\n"
    )
    _add_distribution_file(distribution, "toy_runtime/forged_helper.py", helper_content)
    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )
    monkeypatch.syspath_prepend(str(distribution.locate_file("")))
    forged_module = importlib.import_module(helper_name)
    plugin_source = composition.registries.sources.entries[
        SourceKey(SourceRole.PLUGIN, "toy.runtime")
    ].snapshot
    forged_provenance = ExecutableProvenance.create(
        kind=ExecutableKind.TASK_HANDLER,
        registry_id="toy.runtime.greet",
        owner_id="toy.runtime",
        source_key=SourceKey(SourceRole.PLUGIN, "toy.runtime"),
        source_digest=plugin_source.digest,
        module=ExecutableModuleProvenance(
            module_name=helper_name,
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="toy_runtime/forged_helper.py",
            authenticated_locations=(),
            physical_sha256=hashlib.sha256(helper_content).hexdigest(),
            source_digest=plugin_source.digest,
        ),
        callable_path="toy_runtime.forged_helper:Handler.execute",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )
    descriptor = composition.descriptors[0]
    forged_authority_set = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id="toy.runtime",
        source_key=SourceKey(SourceRole.PLUGIN, "toy.runtime"),
        source_digest=plugin_source.digest,
        contribution=PluginContribution(
            task_handlers={"toy.runtime.greet": forged_module.handler},
        ),
        authorities=(
            ExecutableAuthority(
                executable=forged_module.handler,
                function=type(forged_module.handler).__dict__["execute"],
                bound_self=forged_module.handler,
                descriptor=type(forged_module.handler).__dict__["execute"],
                provenance=forged_provenance,
            ),
        ),
    )
    forged_entry = TaskHandlerEntry(
        capability_id="toy.runtime.greet",
        owner_id="toy.runtime",
        handler=forged_module.handler,
        provenance=forged_provenance,
        authority=forged_authority_set,
    )
    forged_registries = RegistrySet(
        sources=composition.registries.sources,
        capabilities=CapabilityRegistry(
            entries={"toy.runtime.greet": forged_entry},
            task_handlers={"toy.runtime.greet": forged_module.handler},
            commit_validators={},
            bindings={},
        ),
        schemas=composition.registries.schemas,
        resources=composition.registries.resources,
        effects=composition.registries.effects,
    )
    forged_lock = build_invocation_lock(
        manifest=composition.manifest,
        product_snapshot=forged_registries.sources.entries[
            SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
        ].snapshot,
        descriptors={descriptor.plugin_id: descriptor for descriptor in composition.descriptors},
        dependency_order=composition.lock.dependency_order,
        registries=forged_registries,
        configuration=composition.configuration,
        workflow=composition.workflow,
        engine_snapshot=forged_registries.sources.entries[
            SourceKey(SourceRole.ENGINE, "graph.engine")
        ].snapshot,
        contribution_authorities={"toy.runtime": forged_authority_set},
    )

    with pytest.raises(ValueError, match="provenance is not currently authenticated"):
        FrozenComposition.freeze(
            composition.manifest,
            forged_registries,
            composition.workflow,
            forged_lock,
            descriptors=composition.descriptors,
            configuration=composition.configuration,
            contribution_authorities={"toy.runtime": forged_authority_set},
            providers=composition.providers,
            product_provider=composition.product_provider,
            declarative_sources=composition.declarative_sources,
        )


@pytest.mark.parametrize("missing_kind", ("task_handler", "commit_validator", "effect"))
def test_declared_executable_set_cannot_be_removed_from_a_self_consistent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_kind: str,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=("toy.runtime.validate",),
        schemas=("toy.runtime.intent", "toy.runtime.receipt"),
        effects=("toy.runtime.audit",),
    )
    contribution = PluginContribution(
        task_handlers={"toy.runtime.greet": _Handler()},
        commit_validators={"toy.runtime.validate": _Validator()},
        schemas=(
            SchemaContribution(
                "toy.runtime.intent",
                "application/schema+json",
                b'{"type":"object"}',
            ),
            SchemaContribution(
                "toy.runtime.receipt",
                "application/schema+json",
                b'{"type":"object"}',
            ),
        ),
        effects=(
            EffectRegistration(
                kind="toy.runtime.audit",
                intent_schema_id="toy.runtime.intent",
                receipt_schema_id="toy.runtime.receipt",
                handler=_EffectHandler(),
                policy=EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
            ),
        ),
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=_passive_workflow())),
        plugins={
            "toy.runtime": _PluginProvider(
                "toy.runtime",
                contribution=contribution,
                descriptors=(descriptor,),
            )
        },
    )
    assert product_source is not None
    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )
    capabilities = composition.registries.capabilities
    effects = composition.registries.effects
    if missing_kind == "task_handler":
        capabilities = CapabilityRegistry(
            entries={key: value for key, value in capabilities.entries.items() if key != "toy.runtime.greet"},
            task_handlers={},
            commit_validators=capabilities.commit_validators,
            bindings={},
        )
    elif missing_kind == "commit_validator":
        capabilities = CapabilityRegistry(
            entries={
                key: value for key, value in capabilities.entries.items() if key != "toy.runtime.validate"
            },
            task_handlers=capabilities.task_handlers,
            commit_validators={},
            bindings={},
        )
    else:
        effects = EffectRegistry(entries={})
    forged_registries = RegistrySet(
        sources=composition.registries.sources,
        capabilities=capabilities,
        schemas=composition.registries.schemas,
        resources=composition.registries.resources,
        effects=effects,
    )

    with pytest.raises(ValueError, match="contribution authority"):
        build_invocation_lock(
            manifest=composition.manifest,
            product_snapshot=forged_registries.sources.entries[
                SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
            ].snapshot,
            descriptors={descriptor.plugin_id: descriptor for descriptor in composition.descriptors},
            dependency_order=composition.lock.dependency_order,
            registries=forged_registries,
            configuration=composition.configuration,
            workflow=composition.workflow,
            engine_snapshot=forged_registries.sources.entries[
                SourceKey(SourceRole.ENGINE, "graph.engine")
            ].snapshot,
            contribution_authorities=composition.contribution_authorities,
        )


@pytest.mark.parametrize("registry_kind", ("schema", "resource", "binding"))
@pytest.mark.parametrize("mutation", ("missing", "extra", "value"))
def test_nonexecutable_contribution_authority_rejects_self_consistent_registry_lock_forges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_kind: str,
    mutation: str,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
        schemas=("toy.runtime.schema",),
        resources=("toy.runtime.prompt", "toy.runtime.unused"),
        bindings=("toy.runtime.alias",),
    )
    contribution = PluginContribution(
        task_handlers={"toy.runtime.greet": _Handler()},
        schemas=(
            SchemaContribution(
                "toy.runtime.schema",
                "application/schema+json",
                b'{"type":"object"}',
            ),
        ),
        resources=(
            ResourceContribution("toy.runtime.prompt", "text/plain", b"prompt"),
            ResourceContribution("toy.runtime.unused", "text/plain", b"unused"),
        ),
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.runtime.alias",
                target_capability_id="toy.runtime.greet",
                data={"mode": "strict"},
                resource_ids=("toy.runtime.prompt",),
            ),
        ),
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=_passive_workflow())),
        plugins={
            "toy.runtime": _PluginProvider(
                "toy.runtime",
                contribution=contribution,
                descriptors=(descriptor,),
            )
        },
    )
    assert product_source is not None
    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )
    registries = composition.registries

    if registry_kind == "schema":
        entries = dict(registries.schemas.entries)
        if mutation == "missing":
            del entries["toy.runtime.schema"]
        elif mutation == "extra":
            entries["toy.runtime.extra-schema"] = SchemaEntry.from_content(
                schema_id="toy.runtime.extra-schema",
                owner_id="toy.runtime",
                media_type="application/schema+json",
                content=b"{}",
            )
        else:
            entries["toy.runtime.schema"] = SchemaEntry.from_content(
                schema_id="toy.runtime.schema",
                owner_id="toy.runtime",
                media_type="application/schema+json",
                content=b'{"type":"string"}',
            )
        forged = replace(registries, schemas=SchemaRegistry(entries))
    elif registry_kind == "resource":
        entries = dict(registries.resources.entries)
        if mutation == "missing":
            del entries["toy.runtime.unused"]
        elif mutation == "extra":
            content = b"extra"
            entries["toy.runtime.extra-resource"] = ResourceEntry(
                resource_id="toy.runtime.extra-resource",
                owner_id="toy.runtime",
                media_type="text/plain",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        else:
            content = b"changed"
            entries["toy.runtime.unused"] = ResourceEntry(
                resource_id="toy.runtime.unused",
                owner_id="toy.runtime",
                media_type="text/plain",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        forged = replace(registries, resources=ResourceRegistry(entries))
    else:
        capability_entries = dict(registries.capabilities.entries)
        bindings = dict(registries.capabilities.bindings)
        task_handlers = dict(registries.capabilities.task_handlers)
        target_entry = capability_entries["toy.runtime.greet"]
        assert isinstance(target_entry, TaskHandlerEntry)
        if mutation == "missing":
            del capability_entries["toy.runtime.alias"]
            del bindings["toy.runtime.alias"]
            del task_handlers["toy.runtime.alias"]
        else:
            capability_id = "toy.runtime.extra-alias" if mutation == "extra" else "toy.runtime.alias"
            entry = CapabilityBindingEntry._from_target(
                capability_id=capability_id,
                owner_id="toy.runtime",
                target_capability_id="toy.runtime.greet",
                data={"mode": "forged"},
                resource_ids=("toy.runtime.prompt",),
                target=target_entry.handler,
                target_provenance=target_entry.provenance,
            )
            capability_entries[capability_id] = entry
            bindings[capability_id] = entry
            task_handlers[capability_id] = entry.handler
        forged = replace(
            registries,
            capabilities=CapabilityRegistry(
                entries=capability_entries,
                task_handlers=task_handlers,
                commit_validators=registries.capabilities.commit_validators,
                bindings=bindings,
            ),
        )

    with pytest.raises(ValueError, match="contribution authority"):
        build_invocation_lock(
            manifest=composition.manifest,
            product_snapshot=forged.sources.entries[
                SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
            ].snapshot,
            descriptors={item.plugin_id: item for item in composition.descriptors},
            dependency_order=composition.lock.dependency_order,
            registries=forged,
            configuration=composition.configuration,
            workflow=composition.workflow,
            engine_snapshot=forged.sources.entries[SourceKey(SourceRole.ENGINE, "graph.engine")].snapshot,
            contribution_authorities=composition.contribution_authorities,
        )


def test_frozen_composition_rejects_self_consistent_forged_raw_contribution_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
        schemas=("toy.runtime.schema",),
    )
    contribution = PluginContribution(
        task_handlers={"toy.runtime.greet": _Handler()},
        schemas=(
            SchemaContribution(
                "toy.runtime.schema",
                "application/schema+json",
                b'{"type":"object"}',
            ),
        ),
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=_passive_workflow())),
        plugins={
            "toy.runtime": _PluginProvider(
                "toy.runtime",
                contribution=contribution,
                descriptors=(descriptor,),
            )
        },
    )
    assert product_source is not None
    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )
    genuine = composition.contribution_authorities["toy.runtime"]
    forged_raw = PluginContribution(
        task_handlers=genuine.contribution.task_handlers,
        schemas=(
            SchemaContribution(
                "toy.runtime.schema",
                "application/schema+json",
                b'{"type":"string"}',
            ),
        ),
    )
    forged_authority = replace(genuine, contribution=forged_raw)
    task_entry = composition.registries.capabilities.entries["toy.runtime.greet"]
    assert isinstance(task_entry, TaskHandlerEntry)
    forged_task = TaskHandlerEntry(
        capability_id=task_entry.capability_id,
        owner_id=task_entry.owner_id,
        handler=task_entry.handler,
        provenance=task_entry.provenance,
        authority=forged_authority,
    )
    capabilities = CapabilityRegistry(
        entries={"toy.runtime.greet": forged_task},
        task_handlers={"toy.runtime.greet": task_entry.handler},
        commit_validators={},
        bindings={},
    )
    schemas = SchemaRegistry(
        {
            "toy.runtime.schema": SchemaEntry.from_content(
                schema_id="toy.runtime.schema",
                owner_id="toy.runtime",
                media_type="application/schema+json",
                content=b'{"type":"string"}',
            )
        }
    )
    forged_registries = replace(
        composition.registries,
        capabilities=capabilities,
        schemas=schemas,
    )
    forged_lock = build_invocation_lock(
        manifest=composition.manifest,
        product_snapshot=forged_registries.sources.entries[
            SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
        ].snapshot,
        descriptors={item.plugin_id: item for item in composition.descriptors},
        dependency_order=composition.lock.dependency_order,
        registries=forged_registries,
        configuration=composition.configuration,
        workflow=composition.workflow,
        engine_snapshot=forged_registries.sources.entries[
            SourceKey(SourceRole.ENGINE, "graph.engine")
        ].snapshot,
        contribution_authorities={"toy.runtime": forged_authority},
    )

    with pytest.raises(ValueError, match="contribution provenance is not currently authenticated"):
        FrozenComposition.freeze(
            manifest=composition.manifest,
            descriptors=composition.descriptors,
            registries=forged_registries,
            workflow=composition.workflow,
            configuration=composition.configuration,
            contribution_authorities={"toy.runtime": forged_authority},
            providers=composition.providers,
            product_provider=composition.product_provider,
            declarative_sources=composition.declarative_sources,
            lock=forged_lock,
        )


def test_contribution_lazy_import_replaces_unowned_same_path_helper_and_binds_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _LazyContributionProvider("toy.runtime", "toy_runtime.helper")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    helper_path = _add_distribution_file(
        distribution,
        "toy_runtime/helper.py",
        (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'source': 'authenticated'})\n"
            b"handler = Handler()\n"
        ),
    )
    fake_handler = _Handler()
    fake_helper = ModuleType("toy_runtime.helper")
    fake_helper.__file__ = str(helper_path)
    fake_helper.handler = fake_handler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, fake_helper.__name__, fake_helper)

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    handler = composition.registries.capabilities.task_handlers["toy.runtime.greet"]
    assert handler is not fake_handler
    assert type(handler).__module__ == "toy_runtime.helper"
    assert sys.modules["toy_runtime.helper"] is not fake_helper
    authenticated = platform._binding_cache.modules["toy_runtime.helper"]
    assert authenticated.module is sys.modules["toy_runtime.helper"]
    binding = composition.providers["toy.runtime"]
    assert any(
        item.module_name == "toy_runtime.helper"
        for item in binding.import_plan.modules  # type: ignore[union-attr]
    )

    repeated = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))
    assert repeated.providers["toy.runtime"] is binding


def test_same_source_executable_helper_drift_changes_snapshot_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _LazyContributionProvider("toy.runtime", "toy_runtime.drift_helper")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    relative_path = "toy_runtime/drift_helper.py"

    def implementation(marker: str) -> bytes:
        return (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            + f"        return TaskOutcome.succeeded({{'marker': {marker!r}}})\n".encode()
            + b"handler = Handler()\n"
        )

    _add_distribution_file(distribution, relative_path, implementation("first"))
    request = ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    first = platform.resolve(request)
    first_source_digest = first.registries.sources.entries[
        SourceKey(SourceRole.PLUGIN, "toy.runtime")
    ].snapshot.digest

    _replace_distribution_file(distribution, relative_path, implementation("second"))
    second = platform.resolve(request)
    second_source_digest = second.registries.sources.entries[
        SourceKey(SourceRole.PLUGIN, "toy.runtime")
    ].snapshot.digest

    assert second_source_digest != first_source_digest
    assert second.lock.plugins[0].contribution_digest != first.lock.plugins[0].contribution_digest
    assert second.lock.canonical_bytes != first.lock.canonical_bytes
    assert second.lock.digest != first.lock.digest


def test_contribution_quarantines_loaded_leaf_when_its_parent_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "toy_runtime.round5_orphan.helper"
    provider = _LazyContributionProvider("toy.runtime", module_name)
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    helper_path = _add_distribution_file(
        distribution,
        "toy_runtime/round5_orphan/helper.py",
        (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'source': 'authenticated'})\n"
            b"handler = Handler()\n"
        ),
    )
    fake_handler = _Handler()
    fake_helper = ModuleType(module_name)
    fake_helper.__file__ = str(helper_path)
    fake_helper.handler = fake_handler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake_helper)
    assert "toy_runtime.round5_orphan" not in sys.modules

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    handler = composition.registries.capabilities.task_handlers["toy.runtime.greet"]
    assert handler is not fake_handler
    assert type(handler).__module__ == module_name
    assert sys.modules[module_name] is not fake_helper
    assert platform._binding_cache.modules[module_name].module is sys.modules[module_name]


@pytest.mark.parametrize(
    "executable_kind",
    ("task_handler", "commit_validator", "effect"),
)
def test_external_reexported_executable_is_rejected_and_corrected_retry_is_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_kind: str,
) -> None:
    plugin_id = "toy.runtime"
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(f"{plugin_id}.greet",) if executable_kind == "task_handler" else (),
        commit_validators=((f"{plugin_id}.validate",) if executable_kind == "commit_validator" else ()),
        schemas=((f"{plugin_id}.intent", f"{plugin_id}.receipt") if executable_kind == "effect" else ()),
        effects=(f"{plugin_id}.audit",) if executable_kind == "effect" else (),
    )
    external_name = f"round5_external_{executable_kind}"
    selected_name = f"toy_runtime.round5_selected_{executable_kind}"
    provider = _ImportedExecutableProvider(descriptor, external_name, executable_kind)
    workflow = _workflow() if executable_kind == "task_handler" else _passive_workflow()
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=_ProductProvider(_manifest(workflow=workflow)),
        plugins={plugin_id: provider},
    )
    assert product_source is not None
    implementation = (
        b"from graph_engine.plugin_api import (\n"
        b"    EffectApplyResult, EffectReconcileResult, TaskOutcome, ValidationResult,\n"
        b")\n"
        b"class Handler:\n"
        b"    async def execute(self, _request, _context):\n"
        b"        return TaskOutcome.succeeded({'source': 'implementation'})\n"
        b"class Validator:\n"
        b"    def validate(self, _candidate, _context):\n"
        b"        return ValidationResult(accepted=True)\n"
        b"class EffectHandler:\n"
        b"    async def apply(self, _intent, _key):\n"
        b"        return EffectApplyResult.applied({'source': 'implementation'})\n"
        b"    async def reconcile(self, _intent, _key):\n"
        b"        return EffectReconcileResult.applied({'source': 'implementation'})\n"
        b"handler = Handler()\n"
        b"validator = Validator()\n"
        b"effect_handler = EffectHandler()\n"
    )
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / f"{external_name}.py").write_bytes(implementation)
    monkeypatch.syspath_prepend(str(external_root))
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        selected_name.replace(".", "/") + ".py",
        implementation,
    )
    request = ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],))

    with pytest.raises(ResolutionError, match="contribution failed"):
        platform.resolve(request)
    assert external_name not in sys.modules
    assert external_name not in platform._binding_cache.modules

    provider.module_name = selected_name
    corrected = platform.resolve(request)

    assert corrected.providers[plugin_id] is not None
    assert selected_name in platform._binding_cache.modules


@pytest.mark.parametrize(
    "executable_kind",
    ("task_handler", "commit_validator", "effect_apply", "effect_reconcile"),
)
def test_frozen_composition_rejects_same_module_executable_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_kind: str,
) -> None:
    descriptor_kind = "effect" if executable_kind.startswith("effect_") else executable_kind
    plugin_id = "toy.runtime"
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(f"{plugin_id}.greet",) if descriptor_kind == "task_handler" else (),
        commit_validators=(f"{plugin_id}.validate",) if descriptor_kind == "commit_validator" else (),
        schemas=(f"{plugin_id}.intent", f"{plugin_id}.receipt") if descriptor_kind == "effect" else (),
        effects=(f"{plugin_id}.audit",) if descriptor_kind == "effect" else (),
    )
    module_name = f"toy_runtime.same_module_{executable_kind}"
    provider = _ImportedExecutableProvider(descriptor, module_name, descriptor_kind)
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(
            _manifest(workflow=_workflow() if descriptor_kind == "task_handler" else _passive_workflow())
        ),
        plugins={plugin_id: provider},
    )
    assert product_source is not None
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        module_name.replace(".", "/") + ".py",
        (
            b"from graph_engine.plugin_api import (\n"
            b"    EffectApplyResult, EffectReconcileResult, TaskOutcome, ValidationResult,\n"
            b")\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'handler': 'original'})\n"
            b"class AlternateHandler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'handler': 'alternate'})\n"
            b"class Validator:\n"
            b"    def validate(self, _candidate, _context):\n"
            b"        return ValidationResult(accepted=True)\n"
            b"class AlternateValidator:\n"
            b"    def validate(self, _candidate, _context):\n"
            b"        return ValidationResult(accepted=True)\n"
            b"class EffectHandler:\n"
            b"    async def apply(self, _intent, _key):\n"
            b"        return EffectApplyResult.applied({'effect': 'original'})\n"
            b"    async def reconcile(self, _intent, _key):\n"
            b"        return EffectReconcileResult.applied({'effect': 'original'})\n"
            b"class AlternateEffectHandler:\n"
            b"    async def apply(self, _intent, _key):\n"
            b"        return EffectApplyResult.applied({'effect': 'alternate'})\n"
            b"    async def reconcile(self, _intent, _key):\n"
            b"        return EffectReconcileResult.applied({'effect': 'alternate'})\n"
            b"handler = Handler()\n"
            b"alternate_handler = AlternateHandler()\n"
            b"validator = Validator()\n"
            b"alternate_validator = AlternateValidator()\n"
            b"effect_handler = EffectHandler()\n"
            b"alternate_effect_handler = AlternateEffectHandler()\n"
        ),
    )
    composition = platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],)))
    implementation = sys.modules[module_name]
    registries = composition.registries
    if executable_kind == "task_handler":
        entry = registries.capabilities.entries[f"{plugin_id}.greet"]
        assert isinstance(entry, TaskHandlerEntry)
        with pytest.raises(ValueError, match="authority generation"):
            replace(entry, handler=implementation.alternate_handler)
        return
    elif executable_kind == "commit_validator":
        entry = registries.capabilities.entries[f"{plugin_id}.validate"]
        assert isinstance(entry, CommitValidatorEntry)
        with pytest.raises(ValueError, match="authority generation"):
            replace(entry, validator=implementation.alternate_validator)
        return
    else:
        effect = registries.effects.entries[f"{plugin_id}.audit"]
        method_name = "apply" if executable_kind == "effect_apply" else "reconcile"
        setattr(
            effect.handler,
            method_name,
            getattr(implementation.alternate_effect_handler, method_name),
        )

    with pytest.raises(ValueError, match="provenance is not currently authenticated"):
        FrozenComposition.freeze(
            composition.manifest,
            registries,
            composition.workflow,
            composition.lock,
            descriptors=composition.descriptors,
            configuration=composition.configuration,
            contribution_authorities=composition.contribution_authorities,
            providers=composition.providers,
            product_provider=composition.product_provider,
            declarative_sources=composition.declarative_sources,
        )


def test_dynamic_executable_descriptor_is_rejected_before_registry_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
    )
    module_name = "toy_runtime.dynamic_descriptor"
    provider = _ImportedExecutableProvider(descriptor, module_name, "task_handler")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        "toy_runtime/dynamic_descriptor.py",
        (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class RealHandler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'ok': True})\n"
            b"real_handler = RealHandler()\n"
            b"class DynamicHandler:\n"
            b"    def __getattribute__(self, name):\n"
            b"        if name == 'execute':\n"
            b"            return real_handler.execute\n"
            b"        return object.__getattribute__(self, name)\n"
            b"handler = DynamicHandler()\n"
        ),
    )

    with pytest.raises(ResolutionError, match="contribution failed"):
        platform.resolve(ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],)))


@pytest.mark.parametrize(
    ("executable_kind", "slot"),
    (
        ("task_handler", "execute"),
        ("commit_validator", "validate"),
        ("effect", "apply"),
        ("effect", "reconcile"),
    ),
)
def test_stateful_attribute_dispatch_cannot_switch_an_authenticated_callable_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_kind: str,
    slot: str,
) -> None:
    plugin_id = "toy.runtime"
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(f"{plugin_id}.greet",) if executable_kind == "task_handler" else (),
        commit_validators=(f"{plugin_id}.validate",) if executable_kind == "commit_validator" else (),
        schemas=(f"{plugin_id}.intent", f"{plugin_id}.receipt") if executable_kind == "effect" else (),
        effects=(f"{plugin_id}.audit",) if executable_kind == "effect" else (),
    )
    external_name = f"round5_stateful_external_{slot}"
    module_name = f"toy_runtime.stateful_dispatch_{slot}"
    provider = _ImportedExecutableProvider(descriptor, module_name, executable_kind)
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=_ProductProvider(
            _manifest(workflow=_workflow() if executable_kind == "task_handler" else _passive_workflow())
        ),
        plugins={plugin_id: provider},
    )
    assert product_source is not None
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / f"{external_name}.py").write_bytes(
        (
            b"async def execute(_request, _context):\n    return None\n"
            b"def validate(_candidate, _context):\n    return None\n"
            b"async def apply(_intent, _key):\n    return None\n"
            b"async def reconcile(_intent, _key):\n    return None\n"
        )
    )
    monkeypatch.syspath_prepend(str(external_root))
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        module_name.replace(".", "/") + ".py",
        (
            f"import {external_name} as external\n"
            "from graph_engine.plugin_api import (\n"
            "    EffectApplyResult, EffectReconcileResult, TaskOutcome, ValidationResult,\n"
            ")\n"
            "class Handler:\n"
            "    armed = False\n"
            "    def __getattribute__(self, name):\n"
            "        if name == 'execute' and object.__getattribute__(self, 'armed'):\n"
            "            return external.execute\n"
            "        return object.__getattribute__(self, name)\n"
            "    async def execute(self, _request, _context):\n"
            "        return TaskOutcome.succeeded({'source': 'authenticated'})\n"
            "class Validator:\n"
            "    armed = False\n"
            "    def __getattribute__(self, name):\n"
            "        if name == 'validate' and object.__getattribute__(self, 'armed'):\n"
            "            return external.validate\n"
            "        return object.__getattribute__(self, name)\n"
            "    def validate(self, _candidate, _context):\n"
            "        return ValidationResult(accepted=True)\n"
            "class EffectHandler:\n"
            "    armed = False\n"
            "    armed_slot = ''\n"
            "    def __getattribute__(self, name):\n"
            "        if (\n"
            "            name in {'apply', 'reconcile'}\n"
            "            and object.__getattribute__(self, 'armed')\n"
            "            and name == object.__getattribute__(self, 'armed_slot')\n"
            "        ):\n"
            "            return getattr(external, name)\n"
            "        return object.__getattribute__(self, name)\n"
            "    async def apply(self, _intent, _key):\n"
            "        return EffectApplyResult.applied({'source': 'authenticated'})\n"
            "    async def reconcile(self, _intent, _key):\n"
            "        return EffectReconcileResult.applied({'source': 'authenticated'})\n"
            "handler = Handler()\n"
            "validator = Validator()\n"
            "effect_handler = EffectHandler()\n"
        ).encode(),
    )

    with pytest.raises(ResolutionError, match="contribution failed"):
        composition = platform.resolve(
            ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],))
        )
        if executable_kind == "task_handler":
            executable = composition.registries.capabilities.task_handlers[f"{plugin_id}.greet"]
        elif executable_kind == "commit_validator":
            executable = composition.registries.capabilities.commit_validators[f"{plugin_id}.validate"]
        else:
            executable = composition.registries.effects.entries[f"{plugin_id}.audit"].handler
            executable.armed_slot = slot
        executable.armed = True
        runtime_callable = getattr(executable, slot)
        assert runtime_callable.__globals__ is sys.modules[external_name].__dict__
        raise AssertionError("stateful dispatch escaped authenticated callable membership")


def test_module_subclass_dispatch_cannot_switch_an_authenticated_callable_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "toy.runtime"
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(f"{plugin_id}.greet",),
        commit_validators=(),
    )
    external_name = "round5_stateful_external_module"
    module_name = "toy_runtime.stateful_module_dispatch"
    provider = _ImportedExecutableProvider(descriptor, module_name, "task_handler")
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={plugin_id: provider},
    )
    assert product_source is not None
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / f"{external_name}.py").write_bytes(
        b"async def execute(_request, _context):\n    return None\n"
    )
    monkeypatch.syspath_prepend(str(external_root))
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        module_name.replace(".", "/") + ".py",
        (
            f"import {external_name} as external\n"
            "import sys\n"
            "from types import ModuleType\n"
            "from graph_engine.plugin_api import TaskOutcome\n"
            "async def execute(_request, _context):\n"
            "    return TaskOutcome.succeeded({'source': 'authenticated'})\n"
            "class StatefulModule(ModuleType):\n"
            "    def __getattribute__(self, name):\n"
            "        if name == 'execute' and ModuleType.__getattribute__(self, 'armed'):\n"
            "            return external.execute\n"
            "        return ModuleType.__getattribute__(self, name)\n"
            "current = sys.modules[__name__]\n"
            "current.__class__ = StatefulModule\n"
            "current.armed = False\n"
            "current.handler = current\n"
        ).encode(),
    )

    with pytest.raises(ResolutionError, match="contribution failed"):
        composition = platform.resolve(
            ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],))
        )
        executable = composition.registries.capabilities.task_handlers[f"{plugin_id}.greet"]
        executable.armed = True
        runtime_callable = executable.execute
        assert runtime_callable.__globals__ is sys.modules[external_name].__dict__
        raise AssertionError("module-subclass dispatch escaped authenticated callable membership")


@pytest.mark.parametrize("descriptor_kind", ("staticmethod", "classmethod"))
@pytest.mark.parametrize(
    ("executable_kind", "slot"),
    (
        ("task_handler", "execute"),
        ("commit_validator", "validate"),
        ("effect", "apply"),
        ("effect", "reconcile"),
    ),
)
def test_descriptor_subclass_cannot_switch_an_authenticated_callable_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_kind: str,
    executable_kind: str,
    slot: str,
) -> None:
    plugin_id = "toy.runtime"
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(f"{plugin_id}.greet",) if executable_kind == "task_handler" else (),
        commit_validators=(f"{plugin_id}.validate",) if executable_kind == "commit_validator" else (),
        schemas=(f"{plugin_id}.intent", f"{plugin_id}.receipt") if executable_kind == "effect" else (),
        effects=(f"{plugin_id}.audit",) if executable_kind == "effect" else (),
    )
    external_name = f"round5_descriptor_external_{descriptor_kind}_{slot}"
    module_name = f"toy_runtime.descriptor_{descriptor_kind}_{slot}"
    provider = _ImportedExecutableProvider(descriptor, module_name, executable_kind)
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=_ProductProvider(
            _manifest(workflow=_workflow() if executable_kind == "task_handler" else _passive_workflow())
        ),
        plugins={plugin_id: provider},
    )
    assert product_source is not None
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / f"{external_name}.py").write_text(
        "async def execute(*_args):\n    return None\n"
        "def validate(*_args):\n    return None\n"
        "async def apply(*_args):\n    return None\n"
        "async def reconcile(*_args):\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(external_root))
    descriptor_base = "staticmethod" if descriptor_kind == "staticmethod" else "classmethod"
    first_parameter = "" if descriptor_kind == "staticmethod" else "_cls, "
    selected_definition = {
        "execute": (
            f"    async def execute({first_parameter}_request, _context):\n"
            "        return TaskOutcome.succeeded({'source': 'authenticated'})\n"
        ),
        "validate": (
            f"    def validate({first_parameter}_candidate, _context):\n"
            "        return ValidationResult(accepted=True)\n"
        ),
        "apply": (
            f"    async def apply({first_parameter}_intent, _key):\n"
            "        return EffectApplyResult.applied({'source': 'authenticated'})\n"
        ),
        "reconcile": (
            f"    async def reconcile({first_parameter}_intent, _key):\n"
            "        return EffectReconcileResult.applied({'source': 'authenticated'})\n"
        ),
    }[slot]
    other_effect_method = (
        "    async def reconcile(self, _intent, _key):\n"
        "        return EffectReconcileResult.applied({'source': 'authenticated'})\n"
        if slot == "apply"
        else (
            "    async def apply(self, _intent, _key):\n"
            "        return EffectApplyResult.applied({'source': 'authenticated'})\n"
            if slot == "reconcile"
            else ""
        )
    )
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        module_name.replace(".", "/") + ".py",
        (
            f"import {external_name} as external\n"
            "from graph_engine.plugin_api import (\n"
            "    EffectApplyResult, EffectReconcileResult, TaskOutcome, ValidationResult,\n"
            ")\n"
            f"class StatefulDescriptor({descriptor_base}):\n"
            "    armed = False\n"
            "    def __get__(self, instance, owner=None):\n"
            f"        if self.armed:\n            return external.{slot}\n"
            "        return super().__get__(instance, owner)\n"
            "class Executable:\n"
            "    @StatefulDescriptor\n"
            + selected_definition
            + other_effect_method
            + f"descriptor = Executable.__dict__[{slot!r}]\n"
            "handler = Executable()\n"
            "validator = handler\n"
            "effect_handler = handler\n"
        ).encode(),
    )

    with pytest.raises(ResolutionError, match="contribution failed"):
        composition = platform.resolve(
            ResolutionRequest(product=product_source, plugins=(plugins[plugin_id],))
        )
        implementation = sys.modules[module_name]
        implementation.descriptor.armed = True
        if executable_kind == "task_handler":
            executable = composition.registries.capabilities.task_handlers[f"{plugin_id}.greet"]
        elif executable_kind == "commit_validator":
            executable = composition.registries.capabilities.commit_validators[f"{plugin_id}.validate"]
        else:
            executable = composition.registries.effects.entries[f"{plugin_id}.audit"].handler
        runtime_callable = getattr(executable, slot)
        assert runtime_callable.__globals__ is sys.modules[external_name].__dict__
        raise AssertionError("descriptor subclass escaped authenticated callable membership")


def test_same_source_class_reexport_has_one_canonical_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
    )
    provider = _ImportedExecutableProvider(
        descriptor,
        "toy_runtime.provider_exports",
        "task_handler",
    )
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    _add_distribution_file(
        distribution,
        "toy_runtime/impl.py",
        (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'source': 'impl'})\n"
        ),
    )
    _add_distribution_file(
        distribution,
        "toy_runtime/provider_exports.py",
        b"from .impl import Handler\nhandler = Handler()\n",
    )

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    assert (
        type(composition.registries.capabilities.task_handlers["toy.runtime.greet"]).__module__
        == "toy_runtime.impl"
    )


def test_repeated_fresh_contributions_preserve_each_frozen_authority_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FreshInstanceProvider("toy.runtime")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    request = ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    first = platform.resolve(request)
    second = platform.resolve(request)
    assert (
        first.registries.capabilities.task_handlers["toy.runtime.greet"]
        is not second.registries.capabilities.task_handlers["toy.runtime.greet"]
    )

    for composition in (first, second):
        FrozenComposition.freeze(
            composition.manifest,
            composition.registries,
            composition.workflow,
            composition.lock,
            descriptors=composition.descriptors,
            configuration=composition.configuration,
            contribution_authorities=composition.contribution_authorities,
            providers=composition.providers,
            product_provider=composition.product_provider,
            declarative_sources=composition.declarative_sources,
        )


def test_frozen_composition_rejects_mixed_executable_authority_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=("toy.runtime.validate",),
    )
    provider = _FreshInstanceProvider("toy.runtime", descriptors=(descriptor,))
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    request = ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    first = platform.resolve(request)
    second = platform.resolve(request)
    task_entry = first.registries.capabilities.entries["toy.runtime.greet"]
    validator_entry = second.registries.capabilities.entries["toy.runtime.validate"]
    assert isinstance(task_entry, TaskHandlerEntry)
    assert isinstance(validator_entry, CommitValidatorEntry)
    mixed_capabilities = CapabilityRegistry(
        entries={
            task_entry.capability_id: task_entry,
            validator_entry.capability_id: validator_entry,
        },
        task_handlers={task_entry.capability_id: task_entry.handler},
        commit_validators={validator_entry.capability_id: validator_entry.validator},
        bindings={},
    )
    mixed_registries = replace(first.registries, capabilities=mixed_capabilities)
    with pytest.raises(ValueError, match="contribution authority"):
        build_invocation_lock(
            manifest=first.manifest,
            product_snapshot=mixed_registries.sources.entries[
                SourceKey(SourceRole.PRODUCT, first.manifest.product_id)
            ].snapshot,
            descriptors={item.plugin_id: item for item in first.descriptors},
            dependency_order=first.lock.dependency_order,
            registries=mixed_registries,
            configuration=first.configuration,
            workflow=first.workflow,
            engine_snapshot=mixed_registries.sources.entries[
                SourceKey(SourceRole.ENGINE, "graph.engine")
            ].snapshot,
            contribution_authorities=first.contribution_authorities,
        )


def test_failed_contribution_lazy_import_rolls_back_before_corrected_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _LazyContributionProvider("toy.runtime", "toy_runtime.retry_helper")
    provider.fail = True
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=_ProductProvider(_manifest()),
        plugins={"toy.runtime": provider},
    )
    assert product_source is not None
    distribution = platform._metadata_provider.distribution("toy-runtime")
    _add_distribution_file(
        distribution,
        "toy_runtime/retry_helper.py",
        (
            b"from graph_engine.plugin_api import TaskOutcome\n"
            b"class Handler:\n"
            b"    async def execute(self, _request, _context):\n"
            b"        return TaskOutcome.succeeded({'source': 'retry'})\n"
            b"handler = Handler()\n"
        ),
    )
    request = ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))

    with pytest.raises(ResolutionError, match="contribution failed"):
        platform.resolve(request)
    assert "toy_runtime.retry_helper" not in sys.modules
    assert "toy_runtime.retry_helper" not in platform._binding_cache.modules

    provider.fail = False
    corrected = platform.resolve(request)

    handler = corrected.registries.capabilities.task_handlers["toy.runtime.greet"]
    assert type(handler).__module__ == "toy_runtime.retry_helper"
    assert (
        platform._binding_cache.modules["toy_runtime.retry_helper"].module
        is sys.modules["toy_runtime.retry_helper"]
    )


def test_first_live_manifest_and_descriptor_may_lazy_import_authenticated_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _LazyManifestProvider(_manifest(), "toy_product.manifest_helper")
    plugin = _LazyDescriptorProvider("toy.runtime", "toy_runtime.descriptor_helper")
    platform, plugins, product_source = _platform(
        tmp_path,
        monkeypatch,
        product=product,
        plugins={"toy.runtime": plugin},
    )
    assert product_source is not None
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-product"),
        "toy_product/manifest_helper.py",
        b"VALUE = 'manifest'\n",
    )
    _add_distribution_file(
        platform._metadata_provider.distribution("toy-runtime"),
        "toy_runtime/descriptor_helper.py",
        b"VALUE = 'descriptor'\n",
    )
    product.lazy = True
    plugin.lazy = True

    composition = platform.resolve(
        ResolutionRequest(product=product_source, plugins=(plugins["toy.runtime"],))
    )

    assert composition.manifest.product_id == "toy.a"
    assert "toy_product.manifest_helper" in platform._binding_cache.modules
    assert "toy_runtime.descriptor_helper" in platform._binding_cache.modules


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
        import_roots=("",),
    )
    plugin_source_expectation = ProviderSource(
        distribution="toy-combined",
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=domain_id,
        entrypoint_value="toy_combined.plugin:provider",
        declaration_path=plugin_path,
        import_roots=("",),
    )
    product._manifest = product._manifest.model_copy(update={"source": product_source_expectation})
    plugin._descriptors = tuple(
        descriptor.model_copy(update={"source": plugin_source_expectation})
        for descriptor in plugin._descriptors
    )
    plugin._implementation_module = "toy_combined.plugin"
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
    monkeypatch.syspath_prepend(str(paths["toy_combined"].parents[1]))

    def load(entrypoint: metadata.EntryPoint) -> object:
        assert sys.modules["toy_combined"].__file__ == str(paths["toy_combined"])
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

    assert composition.product_provider._provider is product
    assert composition.providers[domain_id]._provider is plugin


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
    assert set(first.contribution_authorities) == {"toy.flow", "toy.runtime"}
    assert first.contribution_authorities["toy.flow"].keys == ()
    with pytest.raises(ValueError, match="selected plugin contribution authorities"):
        replace(
            first,
            contribution_authorities={"toy.runtime": first.contribution_authorities["toy.runtime"]},
        )


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
    assert corrected.providers["toy.runtime"]._provider is provider


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
    effect_entry = composition.registries.effects.entries["toy.runtime.audit"]
    assert effect_projection == [
        {
            "apply_implementation": effect_entry.apply_provenance.projection(),
            "apply_implementation_digest": effect_entry.apply_provenance.digest,
            "intent_schema_id": "toy.runtime.intent",
            "kind": "toy.runtime.audit",
            "owner_id": "toy.runtime",
            "policy": {
                "backoff_seconds": 0.25,
                "max_attempts": 2,
                "timeout_seconds": 1.5,
            },
            "receipt_schema_id": "toy.runtime.receipt",
            "reconcile_implementation": effect_entry.reconcile_provenance.projection(),
            "reconcile_implementation_digest": effect_entry.reconcile_provenance.digest,
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


def test_wheel_product_plus_explicit_config_tree_resolves_without_baked_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config"
    _write_lock_matrix_config_plugin(config_root)
    product = _ProductProvider(_manifest())
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=product,
        plugins={"toy.runtime": _PluginProvider("toy.runtime")},
    )
    assert product_source is not None
    assert product.manifest().config_plugin_paths == ()
    composition = platform.resolve(
        ResolutionRequest(
            product=product_source,
            plugins=(plugins["toy.runtime"], ConfigTreePluginSource(path=config_root)),
        )
    )
    assert composition.lock.product.source.kind.value == "wheel_product"
    assert composition.manifest.config_plugin_paths == (str(config_root.resolve()),)
    assert "toy.config" in {requirement.plugin_id for requirement in composition.manifest.plugins}


def _write_lock_matrix_config_plugin(path: Path) -> None:
    path.mkdir()
    (path / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "plugin_id": "toy.config",
                "plugin_version": "1.0.0",
                "engine_api": ENGINE_API_VERSION,
                "dependencies": [],
                "files": [],
                "bindings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _lock_matrix_contribution(
    *,
    schema: bytes = b'{"type":"object"}',
    resource: bytes = b"base resource\n",
    binding_data: object = None,
) -> PluginContribution:
    return PluginContribution(
        task_handlers={"toy.runtime.greet": _Handler()},
        schemas=(SchemaContribution("toy.runtime.schema", "application/schema+json", schema),),
        resources=(ResourceContribution("toy.runtime.resource", "text/plain", resource),),
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.runtime.alias",
                target_capability_id="toy.runtime.greet",
                data={"mode": "base"} if binding_data is None else binding_data,
                resource_ids=("toy.runtime.resource",),
            ),
        ),
    )


def _lock_matrix_descriptor(
    *,
    version: str = "1.0.0",
    dependencies: tuple[PluginDependency, ...] = (),
) -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version=version,
        engine_api=ENGINE_API_VERSION,
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
        dependencies=dependencies,
        schemas=("toy.runtime.schema",),
        resources=("toy.runtime.resource",),
        bindings=("toy.runtime.alias",),
    )


def _refresh_lock_matrix_product_distribution(
    distribution: metadata.Distribution,
    provider: _ProductProvider,
) -> None:
    manifest = provider._manifest
    assert manifest.source is not None
    _replace_distribution_file(
        distribution,
        "toy_product/product-declaration.json",
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "product",
                "source": manifest.source.model_dump(mode="json"),
                "manifest": _manifest_declaration(manifest),
            }
        ),
    )


def _refresh_lock_matrix_plugin_distribution(
    distribution: metadata.Distribution,
    provider: _PluginProvider,
) -> None:
    descriptor = provider._descriptors[0]
    assert descriptor.source is not None
    _replace_distribution_file(
        distribution,
        "toy_runtime/plugin-declaration.json",
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "plugin",
                "source": descriptor.source.model_dump(mode="json"),
                "descriptor": descriptor.model_dump(mode="json"),
            }
        ),
    )


def _locked_file_digest(files: object, suffix: str) -> str:
    matches = [item.sha256 for item in files if item.path.endswith(suffix)]  # type: ignore[union-attr]
    assert len(matches) == 1
    return matches[0]


def _lock_matrix_facets(composition: FrozenComposition) -> dict[str, object]:
    lock = composition.lock
    plugins = {plugin.plugin_id: plugin for plugin in lock.plugins}
    runtime = plugins["toy.runtime"]
    config = plugins["toy.config"]
    engine_identity = cast(dict[str, object], thaw_json(lock.engine.identity))
    manifest = cast(dict[str, object], thaw_json(lock.product.manifest))
    compiled = cast(dict[str, object], thaw_json(lock.compiled_workflow))
    binding_projection = cast(list[dict[str, JSONValue]], thaw_json(lock.capability_bindings))
    semantic_bindings = [
        {
            key: value
            for key, value in binding.items()
            if key not in {"implementation_digest", "target_implementation"}
        }
        for binding in binding_projection
    ]
    return {
        "engine_code": tuple((item.path, item.sha256) for item in lock.engine.files),
        "engine_version": engine_identity["version"],
        "product_code": _locked_file_digest(lock.product.source.files, "toy_product/__init__.py"),
        "product_version": lock.product.product_version,
        "plugin_code": _locked_file_digest(runtime.source.files, "toy_runtime/__init__.py"),
        "plugin_version": runtime.plugin_version,
        "config_bytes": config.source.digest,
        "dependencies": tuple(
            (dependency.plugin_id, dependency.version_specifier) for dependency in runtime.dependencies
        ),
        "schema": lock.registry_digests.schemas,
        "resource": lock.registry_digests.resources,
        "binding": canonical_digest(cast(JSONValue, semantic_bindings)),
        "validated_config": lock.configuration_digest,
        "graph_definition": canonical_digest(cast(JSONValue, manifest["workflow"])),
        "compiled_artifact": lock.compiled_workflow_digest,
        "entrypoint_map": canonical_digest(cast(JSONValue, compiled["entrypoints"])),
    }


_LOCK_MATRIX_ALLOWED_CHANGES = {
    "engine_code": {"engine_code"},
    "engine_version": {"engine_version"},
    "product_code": {"product_code"},
    "product_version": {"product_version"},
    "plugin_code": {"plugin_code"},
    "plugin_version": {"plugin_version"},
    "config_bytes": {"config_bytes"},
    "dependencies": {"dependencies"},
    "schema": {"schema"},
    "resource": {"resource"},
    "binding": {"binding"},
    "validated_config": {"validated_config"},
    "graph_definition": {"graph_definition", "compiled_artifact"},
    "compiled_artifact": {"compiled_artifact"},
    "entrypoint_map": {"graph_definition", "compiled_artifact", "entrypoint_map"},
}


@pytest.mark.parametrize("facet", tuple(_LOCK_MATRIX_ALLOWED_CHANGES))
def test_engine_open_rejects_each_independently_reresolved_lock_facet_without_claim_or_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    facet: str,
) -> None:
    config_root = tmp_path / "config"
    _write_lock_matrix_config_plugin(config_root)
    workflow = _workflow(
        "toy.runtime.greet",
        schemas=("toy.runtime.schema",),
        resources=("toy.runtime.resource",),
    )
    product_provider = _ProductProvider(
        _manifest(
            plugins=(
                PluginRequirement(plugin_id="toy.config", version_specifier="==1.0.0"),
                PluginRequirement(plugin_id="toy.runtime", version_specifier=">=1,<2"),
            ),
            workflow=workflow,
            configuration={"toy.runtime": {"mode": "base"}},
        )
    )
    runtime_provider = _PluginProvider(
        "toy.runtime",
        descriptors=(_lock_matrix_descriptor(),),
        contribution=_lock_matrix_contribution(),
    )
    platform, plugins, product_source = _platform(
        tmp_path / "wheels",
        monkeypatch,
        product=product_provider,
        plugins={"toy.runtime": runtime_provider},
    )
    assert product_source is not None
    metadata_provider = cast(_MetadataProvider, platform._metadata_provider)
    product_distribution = metadata_provider._distributions["toy-product"]
    product_provider._manifest = product_provider._manifest.model_copy(
        update={"config_plugin_paths": (str(config_root.resolve()),)}
    )
    _refresh_lock_matrix_product_distribution(product_distribution, product_provider)
    request = ResolutionRequest(
        product=product_source,
        plugins=(plugins["toy.runtime"], ConfigTreePluginSource(path=config_root)),
    )
    original = platform.resolve(request)
    engine_root = tmp_path / "engine"
    with Engine(engine_root) as engine:
        engine.start(
            original,
            entrypoint="hello",
            invocation_id="facet-drift",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        ).close()
    invocation = engine_root / "invocations" / "facet-drift"
    before_ledger = b"".join(
        path.read_bytes() for path in sorted((invocation / "ledger").glob("[0-9]*.json"))
    )
    runtime_distribution = metadata_provider._distributions["toy-runtime"]

    if facet in {"engine_code", "engine_version"}:
        snapshot = resolver_runtime._capture_engine_snapshot()
        if facet == "engine_code":
            changed = SourceSnapshot.from_identity(
                snapshot.identity,
                (*snapshot.files, SourceFile.from_bytes("facet-engine.txt", b"changed\n")),
            )
        else:
            changed = SourceSnapshot.from_identity(
                replace(snapshot.identity, version="9.9.9"),
                snapshot.files,
            )
        monkeypatch.setattr(resolver_runtime, "_capture_engine_snapshot", lambda: changed)
    elif facet == "product_code":
        path = Path(product_distribution.locate_file("toy_product/__init__.py"))
        _replace_distribution_file(
            product_distribution,
            "toy_product/__init__.py",
            path.read_bytes() + b"# product code drift\n",
        )
    elif facet == "product_version":
        assert product_provider._manifest.source is not None
        source = product_provider._manifest.source.model_copy(update={"version": "1.0.1"})
        product_provider._manifest = product_provider._manifest.model_copy(
            update={"product_version": "1.0.1", "source": source}
        )
        _refresh_lock_matrix_product_distribution(product_distribution, product_provider)
        _replace_distribution_file(
            product_distribution,
            "toy_product-1.0.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: toy-product\nVersion: 1.0.1\n",
        )
        metadata_provider._distributions["toy-product"] = metadata.Distribution.at(product_distribution._path)
    elif facet == "plugin_code":
        path = Path(runtime_distribution.locate_file("toy_runtime/__init__.py"))
        _replace_distribution_file(
            runtime_distribution,
            "toy_runtime/__init__.py",
            path.read_bytes() + b"# plugin code drift\n",
        )
    elif facet == "plugin_version":
        descriptor = runtime_provider._descriptors[0]
        assert descriptor.source is not None
        source = descriptor.source.model_copy(update={"version": "1.0.1"})
        runtime_provider._descriptors = (
            descriptor.model_copy(update={"plugin_version": "1.0.1", "source": source}),
        )
        _refresh_lock_matrix_plugin_distribution(runtime_distribution, runtime_provider)
        _replace_distribution_file(
            runtime_distribution,
            "toy_runtime-1.0.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: toy-runtime\nVersion: 1.0.1\n",
        )
        metadata_provider._distributions["toy-runtime"] = metadata.Distribution.at(runtime_distribution._path)
    elif facet == "config_bytes":
        plugin_yaml = config_root / "plugin.yaml"
        plugin_yaml.write_bytes(plugin_yaml.read_bytes() + b"# config byte drift\n")
    elif facet == "dependencies":
        descriptor = runtime_provider._descriptors[0]
        runtime_provider._descriptors = (
            descriptor.model_copy(update={"dependencies": (PluginDependency("toy.config", "==1.0.0"),)}),
        )
        _refresh_lock_matrix_plugin_distribution(runtime_distribution, runtime_provider)
    elif facet == "schema":
        runtime_provider._contribution = _lock_matrix_contribution(
            schema=b'{"type":"object","required":["changed"]}'
        )
    elif facet == "resource":
        runtime_provider._contribution = _lock_matrix_contribution(resource=b"changed resource\n")
    elif facet == "binding":
        runtime_provider._contribution = _lock_matrix_contribution(binding_data={"mode": "changed"})
    elif facet == "validated_config":
        product_provider._manifest = product_provider._manifest.model_copy(
            update={"configuration": {"toy.runtime": {"mode": "changed"}}}
        )
        _refresh_lock_matrix_product_distribution(product_distribution, product_provider)
    elif facet == "graph_definition":
        document = workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)
        document["graphs"]["root"]["nodes"]["greet"]["input"] = {"changed": True}
        changed_workflow = WorkflowDef.model_validate(document)
        product_provider._manifest = product_provider._manifest.model_copy(
            update={"workflow": changed_workflow}
        )
        _refresh_lock_matrix_product_distribution(product_distribution, product_provider)
    elif facet == "compiled_artifact":
        real_compile = resolver_runtime.compile_workflow

        def compile_with_changed_artifact(
            selected_workflow: WorkflowDef,
            registries: RegistrySet,
        ) -> object:
            compiled = real_compile(selected_workflow, registries)
            changed = compiled.model_copy(update={"name": f"{compiled.name}-changed"})
            payload = cast(
                JSONValue,
                changed.model_dump(mode="json", by_alias=True, exclude={"digest"}),
            )
            return changed.model_copy(update={"digest": canonical_digest(payload)})

        monkeypatch.setattr(resolver_runtime, "compile_workflow", compile_with_changed_artifact)
    else:
        changed_workflow = _workflow(
            "toy.runtime.greet",
            schemas=("toy.runtime.schema",),
            resources=("toy.runtime.resource",),
            entrypoints={"alternate": "root", "hello": "root"},
        )
        product_provider._manifest = product_provider._manifest.model_copy(
            update={
                "entrypoints": dict(changed_workflow.entrypoints),
                "workflow": changed_workflow,
            }
        )
        _refresh_lock_matrix_product_distribution(product_distribution, product_provider)

    for module_name in tuple(sys.modules):
        if module_name in {"toy_product", "toy_runtime"}:
            sys.modules.pop(module_name, None)
    drifted = RegistryPlatform(metadata_provider=metadata_provider).resolve(request)
    original_facets = _lock_matrix_facets(original)
    drifted_facets = _lock_matrix_facets(drifted)
    changed_facets = {name for name, value in original_facets.items() if drifted_facets[name] != value}
    assert facet in changed_facets
    assert changed_facets <= _LOCK_MATRIX_ALLOWED_CHANGES[facet]
    assert drifted.lock.canonical_bytes != original.lock.canonical_bytes
    claims = 0

    def reject_claim(self: Engine, invocation_fd: int) -> int:
        del self, invocation_fd
        nonlocal claims
        claims += 1
        raise AssertionError("runner claim must not be attempted")

    monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
    with Engine(engine_root) as engine, pytest.raises(InvocationDrift):
        engine.open("facet-drift", drifted, authorization=empty_runtime_authorization())

    assert claims == 0
    assert (
        b"".join(path.read_bytes() for path in sorted((invocation / "ledger").glob("[0-9]*.json")))
        == before_ledger
    )
