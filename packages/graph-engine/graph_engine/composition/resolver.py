from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
import tomllib
from typing import Annotated, Literal, Protocol, TypeAlias, cast

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
from graph_engine.composition.dependencies import resolve_dependency_order
from graph_engine.composition.lock import build_invocation_lock
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ExecutableAuthoritySet,
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistrySet,
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceKey,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.registries import _build_registries
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
    capture_explicit_file,
)
from graph_engine.composition.sources import (
    AuthenticatedProviderBinding,
    EditableWheelProductSource,
    EditableWheelPluginSource,
    MetadataProvider,
    WheelPluginSource,
    WheelPluginDeclaration,
    WheelProductDeclaration,
    WheelProductSource,
    _AuthenticatedBindingCache,
    _load_snapshotted_entrypoint_binding,
    _resolve_wheel_snapshot,
    _snapshot_installed_engine_distribution,
)
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import freeze_json
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import (
    FrozenModel,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    validate_contribution,
)


class ResolutionError(GraphEngineError):
    """Raised when explicit sources cannot produce one complete composition."""


ProductSource: TypeAlias = Annotated[
    WheelProductSource | EditableWheelProductSource | ProductFileSource,
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
    descriptor: PluginDescriptor
    declarative: DeclarativePlugin | None

    @property
    def plugin_id(self) -> str:
        return self.descriptor.plugin_id


@dataclass(frozen=True, slots=True)
class _CapturedSources:
    engine: SourceSnapshot
    product: SourceSnapshot
    product_manifest: ProductManifest | None
    plugins: tuple[_PluginCapture, ...]


@dataclass(frozen=True, slots=True)
class _LoadedPlugins:
    descriptors: Mapping[str, PluginDescriptor]
    providers: Mapping[str, AuthenticatedProviderBinding]
    declarative: Mapping[str, DeclarativePlugin]
    captures: Mapping[str, _PluginCapture]


class _DescriptorProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...


class RegistryPlatform:
    """Resolve one explicit request into a closed, immutable composition."""

    def __init__(
        self,
        *,
        metadata_provider: MetadataProvider = metadata,
    ) -> None:
        self._metadata_provider = metadata_provider
        self._binding_cache = _AuthenticatedBindingCache()

    def resolve(self, request: ResolutionRequest) -> FrozenComposition:
        # 1. Strictly parse the request and the data-only product source.
        request = self._parse_request(request)
        seed = self._parse_product(request)

        # 2. Capture every explicit product, plugin, config, and engine source.
        captured = self._snapshot_sources(request, seed)

        # 3. Normalize only the authenticated, data-only product declaration.
        manifest = self._declared_product_manifest(seed, captured)
        manifest = self._extend_manifest_with_explicit_configs(manifest, captured.plugins)

        # 4. Validate the complete exact dependency closure and canonical topology
        # before importing any wheel provider code.
        descriptors = self._declared_plugin_descriptors(captured.plugins)
        dependency_order = resolve_dependency_order(descriptors, manifest.plugins)

        # 5-6. Load only providers selected by the already-validated topology and
        # require their live declarations to equal the authenticated static data.
        product_provider = self._load_product_provider(seed, captured.product, manifest)
        loaded = self._load_plugin_descriptors(
            captured.plugins,
            descriptors,
            dependency_order,
        )

        # 8. Obtain validated frozen wheel/config contributions in dependency order.
        contributions = self._load_contributions(loaded, dependency_order)

        # 9-10. Build exactly five registries; builders close aliases and references.
        registries = _build_registries(
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
            engine_snapshot=captured.engine,
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
        if isinstance(request.product, WheelProductSource | EditableWheelProductSource):
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
            product_manifest: ProductManifest | None = None
        else:
            assert isinstance(seed.source, WheelProductSource | EditableWheelProductSource)
            resolved_product = _resolve_wheel_snapshot(seed.source, self._metadata_provider)
            product_snapshot = resolved_product.snapshot
            declaration = resolved_product.declaration
            if not isinstance(declaration, WheelProductDeclaration):  # pragma: no cover - typed source.
                raise ResolutionError("wheel product source returned a plugin declaration")
            product_manifest = declaration.manifest

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
                captured.append(
                    _PluginCapture(
                        source,
                        declarative.snapshot,
                        declarative.descriptor,
                        declarative,
                    )
                )
            else:
                resolved_plugin = _resolve_wheel_snapshot(source, self._metadata_provider)
                declaration = resolved_plugin.declaration
                if not isinstance(declaration, WheelPluginDeclaration):  # pragma: no cover
                    raise ResolutionError("wheel plugin source returned a product declaration")
                captured.append(
                    _PluginCapture(
                        source,
                        resolved_plugin.snapshot,
                        declaration.descriptor,
                        None,
                    )
                )
        return _CapturedSources(
            engine=engine_snapshot,
            product=product_snapshot,
            product_manifest=product_manifest,
            plugins=tuple(captured),
        )

    def _declared_product_manifest(
        self,
        seed: _ProductSeed,
        captured: _CapturedSources,
    ) -> ProductManifest:
        manifest = (
            _normalize_declarative_product(seed.declarative)
            if seed.declarative is not None
            else captured.product_manifest
        )
        if manifest is None:  # pragma: no cover - captured wheel declarations are mandatory.
            raise ResolutionError("captured product has no static declaration")
        _validate_product_source_identity(manifest, captured.product)
        _validate_engine_api(manifest.engine_api, "product")
        return manifest

    def _load_product_provider(
        self,
        seed: _ProductSeed,
        snapshot: SourceSnapshot,
        manifest: ProductManifest,
    ) -> object | None:
        if seed.declarative is not None:
            return None
        assert isinstance(seed.source, WheelProductSource | EditableWheelProductSource)
        binding = _load_snapshotted_entrypoint_binding(
            seed.source,
            snapshot,
            self._metadata_provider,
            self._binding_cache,
        )
        if binding.declaration != manifest:
            raise ResolutionError("product provider manifest disagrees with static declaration")
        if _call_product_manifest(binding) != manifest or _call_product_manifest(binding) != manifest:
            raise ResolutionError("product provider manifest drifted during resolution")
        return binding

    def _declared_plugin_descriptors(
        self,
        captures: tuple[_PluginCapture, ...],
    ) -> Mapping[str, PluginDescriptor]:
        descriptors: dict[str, PluginDescriptor] = {}
        for capture in captures:
            plugin_id = capture.plugin_id
            if plugin_id in descriptors:
                raise ResolutionError(f"duplicate selected plugin source: {plugin_id}")
            _validate_descriptor_source_identity(capture.descriptor, capture.snapshot)
            descriptors[plugin_id] = capture.descriptor
        return dict(sorted(descriptors.items()))

    def _load_plugin_descriptors(
        self,
        captures: tuple[_PluginCapture, ...],
        descriptors: Mapping[str, PluginDescriptor],
        dependency_order: tuple[str, ...],
    ) -> _LoadedPlugins:
        providers: dict[str, AuthenticatedProviderBinding] = {}
        declarative: dict[str, DeclarativePlugin] = {}
        by_id = {capture.plugin_id: capture for capture in captures}
        for plugin_id in dependency_order:
            capture = by_id[plugin_id]
            if capture.declarative is not None:
                declarative[plugin_id] = capture.declarative
            else:
                assert isinstance(capture.source, WheelPluginSource | EditableWheelPluginSource)
                binding = _load_snapshotted_entrypoint_binding(
                    capture.source,
                    capture.snapshot,
                    self._metadata_provider,
                    self._binding_cache,
                )
                declared = binding.declaration
                if not isinstance(declared, PluginDescriptor):
                    raise ResolutionError(f"plugin provider returned an invalid descriptor: {plugin_id}")
                if declared != descriptors[plugin_id]:
                    raise ResolutionError(
                        f"plugin provider descriptor disagrees with static declaration: {plugin_id}"
                    )
                if (
                    _call_plugin_descriptor(binding, plugin_id) != declared
                    or _call_plugin_descriptor(binding, plugin_id) != declared
                ):
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
                providers[plugin_id] = binding
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
    ) -> tuple[AuthenticatedContribution, ...]:
        ports = RegistryPorts(ENGINE_API_VERSION)
        contributions: list[AuthenticatedContribution] = []
        for plugin_id in dependency_order:
            descriptor = loaded.descriptors[plugin_id]
            declarative = loaded.declarative.get(plugin_id)
            if declarative is not None:
                raw_contribution = declarative.contribution
                snapshot = loaded.captures[plugin_id].snapshot
                contribution = AuthenticatedContribution(
                    owner_id=plugin_id,
                    source_key=SourceKey(SourceRole.CONFIG, plugin_id),
                    source_digest=snapshot.digest,
                    descriptor=descriptor,
                    contribution=raw_contribution,
                    executables=(),
                    authority_set=ExecutableAuthoritySet(
                        provider_binding=None,
                        descriptor=descriptor,
                        owner_id=plugin_id,
                        source_key=SourceKey(SourceRole.CONFIG, plugin_id),
                        source_digest=snapshot.digest,
                        contribution=raw_contribution,
                        authorities=(),
                    ),
                )
            else:
                binding = loaded.providers[plugin_id]
                if _call_plugin_descriptor(binding, plugin_id) != descriptor:
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
                try:
                    contribution = binding.authenticated_contribute(ports)
                except Exception as error:
                    raise ResolutionError(f"plugin contribution failed: {plugin_id}") from error
                if _call_plugin_descriptor(binding, plugin_id) != descriptor:
                    raise ResolutionError(f"plugin provider descriptor drifted: {plugin_id}")
            if not isinstance(contribution, AuthenticatedContribution):
                raise ResolutionError(f"plugin returned an unsupported contribution: {plugin_id}")
            validate_contribution(descriptor, contribution.contribution)
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
    try:
        project = tomllib.loads(first_metadata.files[0].content.decode("utf-8"))["project"]
        distribution = str(project["name"])
        version = str(Version(str(project["version"])))
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError, InvalidVersion) as error:
        raise SourceSnapshotError("editable engine pyproject has invalid identity metadata") from error
    if distribution != "graph-engine":
        raise SourceSnapshotError("editable engine pyproject has the wrong distribution name")
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.ENGINE,
            root=project_root.resolve(strict=True),
            distribution=distribution,
            version=version,
            engine_installation="editable",
        ),
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
        source=None,
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


def _validate_product_source_identity(
    manifest: ProductManifest,
    snapshot: SourceSnapshot,
) -> None:
    identity = snapshot.identity
    expected_id = identity.product_id
    expected_version = identity.product_version or identity.version
    if expected_id is not None and manifest.product_id != expected_id:
        raise ResolutionError("product manifest id disagrees with selected source")
    try:
        selected_version = str(Version(expected_version or ""))
    except InvalidVersion as error:  # pragma: no cover - snapshots already authenticate versions.
        raise ResolutionError("selected product source has an invalid version") from error
    if manifest.product_version != selected_version:
        raise ResolutionError("product manifest version disagrees with selected source")
    if identity.kind in {SourceKind.WHEEL_PRODUCT, SourceKind.EDITABLE_PRODUCT}:
        expected_source = _provider_source_from_identity(identity)
        if manifest.source != expected_source:
            raise ResolutionError("product manifest source disagrees with selected source")
    elif manifest.source is not None:
        raise ResolutionError("declarative product manifest cannot claim a wheel source")


def _validate_descriptor_source_identity(
    descriptor: PluginDescriptor,
    snapshot: SourceSnapshot,
) -> None:
    identity = snapshot.identity
    expected_id = identity.plugin_id
    expected_version = identity.plugin_version or identity.version
    if expected_id is not None and descriptor.plugin_id != expected_id:
        raise ResolutionError("plugin descriptor id disagrees with selected source")
    try:
        selected_version = str(Version(expected_version or ""))
    except InvalidVersion as error:  # pragma: no cover - snapshots already authenticate versions.
        raise ResolutionError("selected plugin source has an invalid version") from error
    if str(Version(descriptor.plugin_version)) != selected_version:
        raise ResolutionError("plugin descriptor version disagrees with selected source")
    if identity.kind in {SourceKind.WHEEL_PLUGIN, SourceKind.EDITABLE_PLUGIN}:
        expected_source = _provider_source_from_identity(identity)
        if descriptor.source != expected_source:
            raise ResolutionError("plugin descriptor source disagrees with selected source")
    elif descriptor.source is not None:
        raise ResolutionError("declarative plugin descriptor cannot claim a wheel source")
    _validate_engine_api(descriptor.engine_api, f"plugin {descriptor.plugin_id}")


def _provider_source_from_identity(identity: SourceIdentity) -> ProviderSource:
    if (
        any(
            value is None
            for value in (
                identity.distribution,
                identity.version,
                identity.entrypoint_group,
                identity.entrypoint_name,
                identity.entrypoint_value,
                identity.declaration_path,
            )
        )
        or not identity.import_roots
    ):
        raise ResolutionError("wheel source identity is incomplete")
    return ProviderSource(
        distribution=cast(str, identity.distribution),
        version=cast(str, identity.version),
        entrypoint_group=cast(
            Literal["graph_engine.products", "graph_engine.plugins"],
            identity.entrypoint_group,
        ),
        entrypoint_name=cast(str, identity.entrypoint_name),
        entrypoint_value=cast(str, identity.entrypoint_value),
        declaration_path=cast(str, identity.declaration_path),
        import_roots=identity.import_roots,
    )


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


def _call_plugin_descriptor(provider: _DescriptorProvider, plugin_id: str) -> PluginDescriptor:
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
