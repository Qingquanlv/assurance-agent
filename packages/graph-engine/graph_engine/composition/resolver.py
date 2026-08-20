from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
from typing import Annotated, TypeAlias, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import Field, model_validator

from graph_engine import ENGINE_API_VERSION
from graph_engine.composition.declarative import (
    ConfigTreePluginSource,
    DeclarativePlugin,
    DeclarativeProduct,
    ProductFileSource,
    load_config_tree,
    load_product_file,
)
from graph_engine.composition.dependencies import DependencyConflict, resolve_dependency_order
from graph_engine.composition.lock import build_invocation_lock
from graph_engine.composition.models import (
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistrySet,
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
)
from graph_engine.composition.registries import build_registries
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
    capture_explicit_file,
)
from graph_engine.composition.sources import (
    EditableWheelPluginSource,
    MetadataProvider,
    WheelPluginSource,
    WheelProductSource,
    _load_snapshotted_entrypoint_binding,
    _snapshot_installed_engine_distribution,
    snapshot_wheel_source,
)
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import freeze_json
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import WorkflowDef, parse_workflow
from graph_engine.plugin_api import (
    FrozenModel,
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
    RegistryPorts,
    validate_contribution,
)


class ResolutionError(GraphEngineError):
    """Raised when explicit sources cannot produce one complete composition."""


ProductSource: TypeAlias = Annotated[
    WheelProductSource | ProductFileSource,
    Field(discriminator="kind"),
]
PluginSource: TypeAlias = Annotated[
    WheelPluginSource | EditableWheelPluginSource | ConfigTreePluginSource,
    Field(discriminator="kind"),
]


class ResolutionRequest(FrozenModel):
    product: ProductSource
    plugins: tuple[PluginSource, ...]

    @model_validator(mode="after")
    def _validate_explicit_sources(self) -> ResolutionRequest:
        keys = tuple(_source_reference_key(source) for source in self.plugins)
        if len(set(keys)) != len(keys):
            raise ValueError("resolution request repeats an explicit plugin source")
        return self


@dataclass(frozen=True, slots=True)
class _ProductSeed:
    source: ProductSource
    declarative: DeclarativeProduct | None


@dataclass(frozen=True, slots=True)
class _PluginCapture:
    source: PluginSource
    snapshot: SourceSnapshot
    declarative: DeclarativePlugin | None

    @property
    def plugin_id(self) -> str:
        identity = self.snapshot.identity
        plugin_id = (
            identity.plugin_id if identity.kind == SourceKind.CONFIG_TREE else identity.entrypoint_name
        )
        if plugin_id is None:  # pragma: no cover - authenticated source identities require it.
            raise ResolutionError("captured plugin source has no plugin id")
        return plugin_id


@dataclass(frozen=True, slots=True)
class _CapturedSources:
    engine: SourceSnapshot
    product: SourceSnapshot
    plugins: tuple[_PluginCapture, ...]


@dataclass(frozen=True, slots=True)
class _LoadedPlugins:
    descriptors: Mapping[str, PluginDescriptor]
    providers: Mapping[str, PluginProvider]
    declarative: Mapping[str, DeclarativePlugin]
    captures: Mapping[str, _PluginCapture]


class RegistryPlatform:
    """Resolve one explicit request into a closed, immutable composition."""

    def __init__(
        self,
        *,
        metadata_provider: MetadataProvider = metadata,
    ) -> None:
        self._metadata_provider = metadata_provider

    def resolve(self, request: ResolutionRequest) -> FrozenComposition:
        # 1. Strictly parse the request and the data-only product source.
        request = self._parse_request(request)
        seed = self._parse_product(request)

        # 2. Capture every explicit product, plugin, config, and engine source.
        captured = self._snapshot_sources(request, seed)

        # 3. Authenticate and normalize the effective product manifest.
        manifest, product_provider = self._load_product(seed, captured.product)

        # 4-6. Load only already-snapshotted trusted providers and freeze descriptors.
        manifest = self._extend_manifest_with_explicit_configs(manifest, captured.plugins)
        _validate_root_source_set(manifest, captured.plugins)
        loaded = self._load_plugin_descriptors(
            captured.plugins,
            manifest.required_plugin_ids,
        )

        # 7. Validate the exact selected source set, constraints, and canonical topology.
        _validate_product_requirements(manifest, loaded.descriptors)
        dependency_order = resolve_dependency_order(
            loaded.descriptors,
            manifest.required_plugin_ids,
        )

        # 8. Obtain validated frozen wheel/config contributions in dependency order.
        contributions = self._load_contributions(loaded, dependency_order)

        # 9-10. Build exactly five registries; builders close aliases and references.
        registries = build_registries(
            sources=(
                captured.engine,
                captured.product,
                *(loaded.captures[plugin_id].snapshot for plugin_id in dependency_order),
            ),
            contributions=contributions,
            dependency_order=dependency_order,
        )

        # 11. Validate and freeze namespaced product configuration.
        configuration = self._validate_configuration(manifest, loaded.descriptors)

        # 12. Resolve a frozen inline/resource workflow and compile every entrypoint.
        workflow = self._compile_product_workflow(manifest, registries)

        # 13-14. Compute canonical projections/digests and construct the immutable lock.
        lock = build_invocation_lock(
            manifest=manifest,
            product_snapshot=captured.product,
            descriptors=loaded.descriptors,
            dependency_order=dependency_order,
            registries=registries,
            configuration=configuration,
            workflow=workflow,
            engine_digest=captured.engine.digest,
        )

        # 15. Return the sole complete composition value; no runtime path was touched.
        return FrozenComposition.freeze(
            manifest,
            registries,
            workflow,
            lock,
            descriptors=tuple(loaded.descriptors[plugin_id] for plugin_id in dependency_order),
            configuration=configuration,
            providers=loaded.providers,
            product_provider=product_provider,
            declarative_sources={
                plugin_id: plugin.snapshot for plugin_id, plugin in loaded.declarative.items()
            },
        )

    def _parse_request(self, request: object) -> ResolutionRequest:
        if isinstance(request, ResolutionRequest):
            return request
        try:
            return ResolutionRequest.model_validate(request)
        except Exception as error:
            raise ResolutionError("invalid resolution request") from error

    def _parse_product(self, request: ResolutionRequest) -> _ProductSeed:
        if isinstance(request.product, ProductFileSource):
            return _ProductSeed(request.product, load_product_file(request.product))
        if isinstance(request.product, WheelProductSource):
            return _ProductSeed(request.product, None)
        raise ResolutionError("unsupported product source kind")

    def _snapshot_sources(
        self,
        request: ResolutionRequest,
        seed: _ProductSeed,
    ) -> _CapturedSources:
        engine_snapshot = _capture_engine_snapshot()
        if seed.declarative is not None:
            product_snapshot = seed.declarative.snapshot
        else:
            assert isinstance(seed.source, WheelProductSource)
            product_snapshot = snapshot_wheel_source(seed.source, self._metadata_provider)

        sources: list[PluginSource] = list(request.plugins)
        if seed.declarative is not None:
            sources.extend(ConfigTreePluginSource(path=path) for path in seed.declarative.config_plugin_paths)
        keys = tuple(_source_reference_key(source) for source in sources)
        if len(set(keys)) != len(keys):
            raise ResolutionError("effective product repeats an explicit config source")

        captured: list[_PluginCapture] = []
        for source in sorted(sources, key=_source_reference_key):
            if isinstance(source, ConfigTreePluginSource):
                declarative = load_config_tree(source)
                captured.append(_PluginCapture(source, declarative.snapshot, declarative))
            else:
                captured.append(
                    _PluginCapture(
                        source,
                        snapshot_wheel_source(source, self._metadata_provider),
                        None,
                    )
                )
        return _CapturedSources(
            engine=engine_snapshot,
            product=product_snapshot,
            plugins=tuple(captured),
        )

    def _load_product(
        self,
        seed: _ProductSeed,
        snapshot: SourceSnapshot,
    ) -> tuple[ProductManifest, object | None]:
        if seed.declarative is not None:
            manifest = _normalize_declarative_product(seed.declarative)
            provider: object | None = None
        else:
            assert isinstance(seed.source, WheelProductSource)
            binding = _load_snapshotted_entrypoint_binding(
                seed.source,
                snapshot,
                self._metadata_provider,
            )
            provider = binding.provider
            first = binding.declaration
            second = _call_product_manifest(provider)
            third = _call_product_manifest(provider)
            if first != second or second != third:
                raise ResolutionError("product provider manifest drifted during resolution")
            manifest = _normalize_wheel_product(first)
        _validate_product_source_identity(manifest, snapshot)
        _validate_engine_api(manifest.engine_api, "product")
        return manifest, provider

    def _load_plugin_descriptors(
        self,
        captures: tuple[_PluginCapture, ...],
        required_plugin_ids: tuple[str, ...],
    ) -> _LoadedPlugins:
        descriptors: dict[str, PluginDescriptor] = {}
        providers: dict[str, PluginProvider] = {}
        declarative: dict[str, DeclarativePlugin] = {}
        by_id: dict[str, _PluginCapture] = {}
        for capture in captures:
            plugin_id = capture.plugin_id
            if plugin_id in by_id:
                raise ResolutionError(f"duplicate selected plugin source: {plugin_id}")
            by_id[plugin_id] = capture

        pending = list(sorted(required_plugin_ids))
        missing_source = False
        while pending:
            plugin_id = pending.pop(0)
            if plugin_id in descriptors:
                continue
            capture = by_id.get(plugin_id)
            if capture is None:
                missing_source = True
                continue
            if capture.declarative is not None:
                descriptor = capture.declarative.descriptor
                declarative[plugin_id] = capture.declarative
            else:
                assert isinstance(capture.source, WheelPluginSource | EditableWheelPluginSource)
                binding = _load_snapshotted_entrypoint_binding(
                    capture.source,
                    capture.snapshot,
                    self._metadata_provider,
                )
                provider = cast(PluginProvider, binding.provider)
                first = binding.declaration
                if not isinstance(first, PluginDescriptor):
                    raise ResolutionError(f"plugin provider returned an invalid descriptor: {plugin_id}")
                second = _call_plugin_descriptor(provider, plugin_id)
                third = _call_plugin_descriptor(provider, plugin_id)
                if first != second or second != third:
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
                descriptor = first
                providers[plugin_id] = provider
            _validate_descriptor_source_identity(descriptor, capture.snapshot)
            descriptors[plugin_id] = descriptor
            _validate_dependency_source_set(descriptor, by_id)
            pending.extend(
                dependency.plugin_id
                for dependency in sorted(
                    descriptor.dependencies,
                    key=lambda dependency: dependency.plugin_id,
                )
                if dependency.plugin_id not in descriptors
            )
            pending.sort()

        if missing_source:
            resolve_dependency_order(descriptors, required_plugin_ids)
            raise AssertionError("dependency validation returned despite a missing source")
        unexpected = tuple(sorted(set(by_id).difference(descriptors)))
        if unexpected:
            raise DependencyConflict(f"unexpected selected plugin source: {', '.join(unexpected)}")
        return _LoadedPlugins(
            descriptors=dict(sorted(descriptors.items())),
            providers=dict(sorted(providers.items())),
            declarative=dict(sorted(declarative.items())),
            captures=dict(sorted(by_id.items())),
        )

    def _extend_manifest_with_explicit_configs(
        self,
        manifest: ProductManifest,
        captures: tuple[_PluginCapture, ...],
    ) -> ProductManifest:
        requirements = {requirement.plugin_id: requirement for requirement in manifest.plugins}
        declarative_plugins = {
            capture.plugin_id: capture.declarative for capture in captures if capture.declarative is not None
        }
        for plugin_id, declarative in declarative_plugins.items():
            assert declarative is not None
            requirements.setdefault(
                plugin_id,
                PluginRequirement(
                    plugin_id=plugin_id,
                    version_specifier=f"=={declarative.descriptor.plugin_version}",
                ),
            )
        config_paths = tuple(
            sorted(
                {
                    *manifest.config_plugin_paths,
                    *(
                        str(plugin.snapshot.identity.root)
                        for plugin in declarative_plugins.values()
                        if plugin is not None
                    ),
                }
            )
        )
        return manifest.model_copy(
            update={
                "plugins": tuple(requirements[plugin_id] for plugin_id in sorted(requirements)),
                "config_plugin_paths": config_paths,
            }
        )

    def _load_contributions(
        self,
        loaded: _LoadedPlugins,
        dependency_order: tuple[str, ...],
    ) -> tuple[PluginContribution, ...]:
        ports = RegistryPorts(ENGINE_API_VERSION)
        contributions: list[PluginContribution] = []
        for plugin_id in dependency_order:
            descriptor = loaded.descriptors[plugin_id]
            declarative = loaded.declarative.get(plugin_id)
            if declarative is not None:
                contribution = declarative.contribution
            else:
                provider = loaded.providers[plugin_id]
                if _call_plugin_descriptor(provider, plugin_id) != descriptor:
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
                try:
                    contribution = provider.contribute(ports)
                except Exception as error:
                    raise ResolutionError(f"plugin contribution failed: {plugin_id}") from error
                if _call_plugin_descriptor(provider, plugin_id) != descriptor:
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
            if type(contribution) is not PluginContribution:
                raise ResolutionError(f"plugin returned an unsupported contribution: {plugin_id}")
            validate_contribution(descriptor, contribution)
            contributions.append(contribution)
        return tuple(contributions)

    def _validate_configuration(
        self,
        manifest: ProductManifest,
        descriptors: Mapping[str, PluginDescriptor],
    ) -> object:
        configuration = freeze_json(manifest.configuration)
        if not isinstance(configuration, Mapping):
            raise ResolutionError("product configuration must be namespaced")
        unknown = tuple(sorted(set(configuration).difference(descriptors)))
        if unknown:
            raise ResolutionError(f"configuration targets an unselected plugin: {unknown[0]}")
        for plugin_id, value in configuration.items():
            if not isinstance(value, Mapping):
                raise ResolutionError(f"configuration for {plugin_id} must be a mapping")
        return configuration

    def _compile_product_workflow(
        self,
        manifest: ProductManifest,
        registries: RegistrySet,
    ) -> CompiledWorkflow:
        if manifest.workflow is not None:
            workflow = manifest.workflow
        else:
            assert manifest.workflow_resource_id is not None
            resource = registries.resources.entries.get(manifest.workflow_resource_id)
            if resource is None:
                raise ResolutionError(
                    f"product workflow resource is not registered: {manifest.workflow_resource_id}"
                )
            if resource.media_type != "application/vnd.graph-engine.workflow+yaml":
                raise ResolutionError("product workflow resource has the wrong media type")
            try:
                workflow = parse_workflow(resource.content.decode("utf-8"))
            except Exception as error:
                raise ResolutionError("product workflow resource is invalid") from error
        if dict(workflow.entrypoints) != dict(manifest.entrypoints):
            raise ResolutionError("product entrypoints disagree with the selected workflow")
        compiled = compile_workflow(workflow, registries)
        if dict(compiled.entrypoints) != dict(manifest.entrypoints):  # pragma: no cover - compiler copies.
            raise ResolutionError("compiled workflow entrypoints disagree with product")
        return compiled


def _capture_engine_snapshot() -> SourceSnapshot:
    package_root = Path(__file__).resolve().parents[1]
    project_root = package_root.parent
    if (project_root / "pyproject.toml").is_file():
        return _capture_editable_engine_snapshot(project_root)
    try:
        distribution = metadata.distribution("graph-engine")
    except metadata.PackageNotFoundError as error:
        raise SourceSnapshotError("installed graph-engine distribution not found") from error
    return _snapshot_installed_engine_distribution(distribution)


def _capture_editable_engine_snapshot(project_root: Path) -> SourceSnapshot:
    package_root = project_root / "graph_engine"
    if not package_root.is_dir():
        raise SourceSnapshotError("editable graph-engine package root is missing")

    first_package = _capture_engine_package_tree(package_root)
    first_metadata = capture_explicit_file(
        project_root,
        "pyproject.toml",
        DeclaredTreePolicy(kind=SourceKind.ENGINE),
    )
    second_package = _capture_engine_package_tree(package_root)
    second_metadata = capture_explicit_file(
        project_root,
        "pyproject.toml",
        DeclaredTreePolicy(kind=SourceKind.ENGINE),
    )
    if first_package.files != second_package.files or first_metadata.files != second_metadata.files:
        raise SourceSnapshotError("editable engine source changed while it was captured")

    package_files = tuple(
        SourceFile.from_bytes(f"graph_engine/{source_file.path}", source_file.content)
        for source_file in first_package.files
        if "__pycache__" not in source_file.path.split("/")
        and not source_file.path.endswith((".pyc", ".pyo"))
    )
    packaging_files = tuple(
        SourceFile.from_bytes(source_file.path, source_file.content) for source_file in first_metadata.files
    )
    return SourceSnapshot.from_identity(
        SourceIdentity(kind=SourceKind.ENGINE, root=project_root.resolve(strict=True)),
        (*package_files, *packaging_files),
    )


def _capture_engine_package_tree(package_root: Path) -> SourceSnapshot:
    files: list[str] = []
    for current_root, directory_names, file_names in os.walk(package_root, followlinks=False):
        directory_names[:] = sorted(directory_names)
        current = Path(current_root)
        for file_name in sorted(file_names):
            files.append((current / file_name).relative_to(package_root).as_posix())
    return capture_declared_tree(
        package_root,
        tuple(files),
        DeclaredTreePolicy(kind=SourceKind.ENGINE),
    )


def _normalize_declarative_product(product: DeclarativeProduct) -> ProductManifest:
    document = product.manifest
    return ProductManifest(
        schema_version=document.schema_version,
        product_id=document.product_id,
        product_version=document.product_version,
        engine_api=document.engine_api,
        plugins=tuple(
            PluginRequirement(
                plugin_id=requirement.plugin_id,
                version_specifier=requirement.version_specifier,
            )
            for requirement in document.plugins
        ),
        entrypoints=document.entrypoints,
        configuration=document.configuration,
        config_plugin_paths=tuple(str(path) for path in product.config_plugin_paths),
        workflow=document.workflow,
        workflow_resource_id=document.workflow_resource_id,
    )


def _normalize_wheel_product(value: object) -> ProductManifest:
    if isinstance(value, ProductManifest):
        return value
    try:
        workflow = cast(WorkflowDef, getattr(value, "workflow"))
        raw_requirements = tuple(getattr(value, "plugins"))
        requirements = tuple(
            PluginRequirement(
                plugin_id=getattr(requirement, "plugin_id"),
                version_specifier=(
                    getattr(requirement, "version_specifier")
                    if hasattr(requirement, "version_specifier")
                    else f"=={Version(getattr(requirement, 'version'))}"
                ),
            )
            for requirement in raw_requirements
        )
        entrypoints = getattr(value, "entrypoints", workflow.entrypoints)
        return ProductManifest(
            schema_version=getattr(value, "schema_version", "1"),
            product_id=getattr(value, "product_id"),
            product_version=getattr(value, "product_version"),
            engine_api=getattr(value, "engine_api"),
            plugins=requirements,
            entrypoints=entrypoints,
            configuration=getattr(value, "configuration", {}),
            config_plugin_paths=tuple(getattr(value, "config_plugin_paths", ())),
            workflow=workflow,
            workflow_resource_id=getattr(value, "workflow_resource_id", None),
        )
    except (AttributeError, InvalidVersion, TypeError, ValueError) as error:
        raise ResolutionError("product provider returned an invalid manifest") from error


def _validate_product_source_identity(
    manifest: ProductManifest,
    snapshot: SourceSnapshot,
) -> None:
    identity = snapshot.identity
    expected_id = identity.product_id or identity.entrypoint_name
    expected_version = identity.product_version or identity.version
    if manifest.product_id != expected_id:
        raise ResolutionError("product manifest id disagrees with selected source")
    try:
        selected_version = str(Version(expected_version or ""))
    except InvalidVersion as error:  # pragma: no cover - snapshots already authenticate versions.
        raise ResolutionError("selected product source has an invalid version") from error
    if manifest.product_version != selected_version:
        raise ResolutionError("product manifest version disagrees with selected source")


def _validate_descriptor_source_identity(
    descriptor: PluginDescriptor,
    snapshot: SourceSnapshot,
) -> None:
    identity = snapshot.identity
    expected_id = identity.plugin_id or identity.entrypoint_name
    expected_version = identity.plugin_version or identity.version
    if descriptor.plugin_id != expected_id:
        raise ResolutionError("plugin descriptor id disagrees with selected source")
    try:
        selected_version = str(Version(expected_version or ""))
    except InvalidVersion as error:  # pragma: no cover - snapshots already authenticate versions.
        raise ResolutionError("selected plugin source has an invalid version") from error
    if str(Version(descriptor.plugin_version)) != selected_version:
        raise ResolutionError("plugin descriptor version disagrees with selected source")
    _validate_engine_api(descriptor.engine_api, f"plugin {descriptor.plugin_id}")


def _validate_product_requirements(
    manifest: ProductManifest,
    descriptors: Mapping[str, PluginDescriptor],
) -> None:
    for requirement in manifest.plugins:
        descriptor = descriptors.get(requirement.plugin_id)
        if descriptor is None:  # The exact-source validator reports the canonical missing-source error.
            continue
        selected_version = Version(descriptor.plugin_version)
        constraint = SpecifierSet(requirement.version_specifier)
        if selected_version not in constraint:
            raise DependencyConflict(
                f"product {manifest.product_id} requires "
                f"{requirement.plugin_id}{requirement.version_specifier}, but selected "
                f"{requirement.plugin_id}=={selected_version}"
            )


def _validate_root_source_set(
    manifest: ProductManifest,
    captures: tuple[_PluginCapture, ...],
) -> None:
    by_id = {capture.plugin_id: capture for capture in captures}
    missing = tuple(
        requirement.plugin_id for requirement in manifest.plugins if requirement.plugin_id not in by_id
    )
    if missing:
        raise DependencyConflict(f"missing selected plugin source: {', '.join(sorted(missing))}")
    for requirement in manifest.plugins:
        selected_version = _captured_plugin_version(by_id[requirement.plugin_id])
        if selected_version not in SpecifierSet(requirement.version_specifier):
            raise DependencyConflict(
                f"product {manifest.product_id} requires "
                f"{requirement.plugin_id}{requirement.version_specifier}, but selected "
                f"{requirement.plugin_id}=={selected_version}"
            )


def _validate_dependency_source_set(
    descriptor: PluginDescriptor,
    captures: Mapping[str, _PluginCapture],
) -> None:
    for dependency in descriptor.dependencies:
        capture = captures.get(dependency.plugin_id)
        if capture is None:
            raise DependencyConflict(f"missing selected plugin source: {dependency.plugin_id}")
        selected_version = _captured_plugin_version(capture)
        if selected_version not in SpecifierSet(dependency.version_specifier):
            raise DependencyConflict(
                f"{descriptor.plugin_id} requires "
                f"{dependency.plugin_id}{dependency.version_specifier}, but selected "
                f"{dependency.plugin_id}=={selected_version}"
            )


def _captured_plugin_version(capture: _PluginCapture) -> Version:
    identity = capture.snapshot.identity
    value = identity.plugin_version or identity.version
    try:
        return Version(value or "")
    except InvalidVersion as error:  # pragma: no cover - snapshot identities normalize versions.
        raise ResolutionError(
            f"selected plugin source has an invalid version: {capture.plugin_id}"
        ) from error


def _validate_engine_api(requirement: str, owner: str) -> None:
    engine = Version(ENGINE_API_VERSION)
    try:
        matches = Version(requirement) == engine
    except InvalidVersion:
        try:
            matches = engine in SpecifierSet(requirement)
        except InvalidSpecifier as error:
            raise ResolutionError(f"{owner} has an invalid engine API requirement") from error
    if not matches:
        raise ResolutionError(
            f"{owner} requires engine API {requirement!r}, but engine is {ENGINE_API_VERSION}"
        )


def _call_product_manifest(provider: object) -> object:
    method = getattr(provider, "manifest", None)
    if not callable(method):
        raise ResolutionError("product provider has no callable manifest")
    try:
        return method()
    except Exception as error:
        raise ResolutionError("product provider manifest failed") from error


def _call_plugin_descriptor(provider: PluginProvider, plugin_id: str) -> PluginDescriptor:
    try:
        descriptor = provider.descriptor()
    except Exception as error:
        raise ResolutionError(f"plugin provider descriptor failed: {plugin_id}") from error
    if not isinstance(descriptor, PluginDescriptor):
        raise ResolutionError(f"plugin provider returned an invalid descriptor: {plugin_id}")
    return descriptor


def _source_reference_key(source: PluginSource) -> tuple[str, ...]:
    if isinstance(source, ConfigTreePluginSource):
        return (source.kind, str(source.path.absolute()))
    if isinstance(source, EditableWheelPluginSource):
        return (
            source.kind,
            source.distribution,
            source.entrypoint_group,
            source.entrypoint_name,
            str(source.source_root.absolute()),
            *source.source_files,
        )
    return (
        source.kind,
        source.distribution,
        source.entrypoint_group,
        source.entrypoint_name,
    )


__all__ = [
    "PluginSource",
    "ProductSource",
    "RegistryPlatform",
    "ResolutionError",
    "ResolutionRequest",
]
