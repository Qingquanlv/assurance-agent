from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re
from pathlib import Path
from typing import Literal, Self, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.composition.contributions import (
    ContributionProjection,
    validate_contribution_projection_set,
    validate_registry_contribution_authorities,
)
from graph_engine.composition.models import (
    CapabilityBindingEntry,
    CommitValidatorEntry,
    ContributionAuthority,
    ProductManifest,
    RegistrySet,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
    TaskHandlerEntry,
)
from graph_engine.composition.dependencies import DependencyConflict, resolve_dependency_order
from graph_engine.frozen_json import FrozenJSONValue, thaw_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel, PluginDescriptor, ProviderSource


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
TASK_HOST_IMPLEMENTATION_ID = "graph.engine.task-host"


TASK_HOST_WIRE_SCHEMA_VERSION: Literal["2"] = "2"


def _validate_provider_source_identity(
    source: ProviderSource | None,
    identity: Mapping[str, object],
    kind: str,
) -> None:
    if source is None:
        raise ValueError(f"locked wheel {kind} declaration lacks a source expectation")
    expected = {
        "distribution": source.distribution,
        "version": source.version,
        "entrypoint_group": source.entrypoint_group,
        "entrypoint_name": source.entrypoint_name,
        "entrypoint_value": source.entrypoint_value,
        "declaration_path": source.declaration_path,
        "import_roots": list(source.import_roots),
    }
    if any(identity.get(name) != value for name, value in expected.items()):
        raise ValueError(f"locked {kind} declaration disagrees with its source identity")


class LockedSourceFile(FrozenModel):
    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value or not isinstance(value, str):
            raise ValueError("locked source file path must be non-empty text")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "locked source file")


class LockedSource(FrozenModel):
    kind: SourceKind
    identity: FrozenJSONValue
    digest: str
    files: tuple[LockedSourceFile, ...]

    @field_validator("identity", mode="after")
    @classmethod
    def _validate_identity(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("locked source identity must be a mapping")
        return value

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _sha256(value, "locked source")

    @model_validator(mode="after")
    def _validate_files(self) -> LockedSource:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("locked source files must have unique canonical order")
        return self


class LockedDependency(FrozenModel):
    plugin_id: str
    version_specifier: str

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _qualified_id(value, "locked dependency plugin id")

    @field_validator("version_specifier")
    @classmethod
    def _validate_version_specifier(cls, value: str) -> str:
        try:
            return str(SpecifierSet(value))
        except InvalidSpecifier as error:
            raise ValueError(f"invalid locked dependency version specifier: {value!r}") from error


class LockedProduct(FrozenModel):
    product_id: str
    product_version: str
    manifest: FrozenJSONValue
    manifest_digest: str
    source: LockedSource

    @field_validator("product_id")
    @classmethod
    def _validate_product_id(cls, value: str) -> str:
        return _qualified_id(value, "locked product id")

    @field_validator("product_version")
    @classmethod
    def _validate_product_version(cls, value: str) -> str:
        return _version(value, "locked product version")

    @field_validator("manifest", mode="after")
    @classmethod
    def _validate_manifest(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("locked product manifest must be a mapping")
        return value

    @field_validator("manifest_digest")
    @classmethod
    def _validate_manifest_digest(cls, value: str) -> str:
        return _sha256(value, "locked product manifest")

    @model_validator(mode="after")
    def _authenticate_manifest(self) -> LockedProduct:
        manifest = cast(JSONValue, thaw_json(self.manifest))
        if canonical_digest(manifest) != self.manifest_digest:
            raise ValueError("locked product manifest digest does not authenticate its projection")
        try:
            parsed_manifest = ProductManifest.model_validate(manifest)
        except (TypeError, ValueError, ValidationError) as error:
            raise ValueError("locked product manifest violates its frozen schema") from error
        if (
            parsed_manifest.product_id != self.product_id
            or parsed_manifest.product_version != self.product_version
        ):
            raise ValueError("locked product fields disagree with its manifest")
        if self.source.kind not in {
            SourceKind.WHEEL_PRODUCT,
            SourceKind.EDITABLE_PRODUCT,
        }:
            raise ValueError("locked product source identity has the wrong source kind")
        identity = cast(dict[str, object], thaw_json(self.source.identity))
        source_id = identity.get("product_id", identity.get("entrypoint_name"))
        source_version = identity.get("product_version", identity.get("version"))
        if source_id is not None and source_id != self.product_id:
            raise ValueError("locked product disagrees with its source identity")
        if source_version is not None and source_version != self.product_version:
            raise ValueError("locked product disagrees with its source identity")
        _validate_provider_source_identity(parsed_manifest.source, identity, "product")
        return self


class LockedPlugin(FrozenModel):
    plugin_id: str
    plugin_version: str
    descriptor: PluginDescriptor
    descriptor_digest: str
    contribution: FrozenJSONValue
    contribution_digest: str
    dependencies: tuple[LockedDependency, ...]
    source: LockedSource

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _qualified_id(value, "locked plugin id")

    @field_validator("plugin_version")
    @classmethod
    def _validate_plugin_version(cls, value: str) -> str:
        return _version(value, "locked plugin version")

    @field_validator("descriptor_digest", "contribution_digest")
    @classmethod
    def _validate_descriptor_digest(cls, value: str) -> str:
        return _sha256(value, "locked plugin descriptor")

    @model_validator(mode="after")
    def _validate_dependencies(self) -> LockedPlugin:
        descriptor_projection = _descriptor_projection(self.descriptor)
        if canonical_digest(descriptor_projection) != self.descriptor_digest:
            raise ValueError("locked plugin descriptor digest does not authenticate its projection")
        if (
            self.descriptor.plugin_id != self.plugin_id
            or self.descriptor.plugin_version != self.plugin_version
        ):
            raise ValueError("locked plugin fields disagree with its descriptor")
        raw_contribution = cast(JSONValue, thaw_json(self.contribution))
        if canonical_digest(raw_contribution) != self.contribution_digest:
            raise ValueError("locked plugin contribution digest does not authenticate its projection")
        expected_role = SourceRole.CONFIG if self.source.kind is SourceKind.CONFIG_TREE else SourceRole.PLUGIN
        contribution = ContributionProjection.model_validate(raw_contribution)
        contribution.validate_selected(
            self.descriptor,
            SourceKey(expected_role, self.plugin_id),
            self.source.digest,
        )
        dependency_ids = tuple(item.plugin_id for item in self.dependencies)
        if dependency_ids != tuple(sorted(dependency_ids)) or len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("locked plugin dependencies must have unique canonical order")
        descriptor_dependencies = tuple(
            (dependency.plugin_id, str(SpecifierSet(dependency.version_specifier)))
            for dependency in sorted(
                self.descriptor.dependencies,
                key=lambda dependency: dependency.plugin_id,
            )
        )
        locked_dependencies = tuple(
            (dependency.plugin_id, dependency.version_specifier) for dependency in self.dependencies
        )
        if locked_dependencies != descriptor_dependencies:
            raise ValueError("locked plugin dependencies disagree with its descriptor")
        if self.source.kind not in {
            SourceKind.WHEEL_PLUGIN,
            SourceKind.EDITABLE_PLUGIN,
            SourceKind.CONFIG_TREE,
        }:
            raise ValueError("locked plugin source identity has the wrong source kind")
        identity = cast(dict[str, object], thaw_json(self.source.identity))
        source_id = identity.get("plugin_id", identity.get("entrypoint_name"))
        source_version = identity.get("plugin_version", identity.get("version"))
        if source_id is not None and source_id != self.plugin_id:
            raise ValueError("locked plugin disagrees with its source identity")
        if source_version is not None and source_version != self.plugin_version:
            raise ValueError("locked plugin disagrees with its source identity")
        if self.source.kind in {SourceKind.WHEEL_PLUGIN, SourceKind.EDITABLE_PLUGIN}:
            _validate_provider_source_identity(self.descriptor.source, identity, "plugin")
        elif self.descriptor.source is not None:
            raise ValueError("locked declarative plugin cannot claim a wheel source")
        return self


class RegistryDigests(FrozenModel):
    sources: str
    capabilities: str
    schemas: str
    resources: str
    attempt_contracts: str

    @field_validator("sources", "capabilities", "schemas", "resources", "attempt_contracts")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _sha256(value, "registry")


class RegistryProjections(FrozenModel):
    """Canonical, auditable values authenticated by the registry digests."""

    sources: FrozenJSONValue
    capabilities: FrozenJSONValue
    schemas: FrozenJSONValue
    resources: FrozenJSONValue
    attempt_contracts: FrozenJSONValue

    @model_validator(mode="after")
    def _validate_projection_shapes(self) -> RegistryProjections:
        for field_name in ("sources", "capabilities", "schemas", "resources", "attempt_contracts"):
            if not isinstance(getattr(self, field_name), tuple):
                raise ValueError(f"{field_name} registry projection must be a list")
        return self


class ExecutionHostLock(FrozenModel):
    implementation_id: str
    implementation_digest: str
    wire_schema_version: Literal["2"]

    @field_validator("implementation_id")
    @classmethod
    def _validate_implementation_id(cls, value: str) -> str:
        return _qualified_id(value, "execution host implementation id")

    @field_validator("implementation_digest")
    @classmethod
    def _validate_implementation_digest(cls, value: str) -> str:
        return _sha256(value, "execution host implementation")


def pinned_execution_host_lock() -> ExecutionHostLock:
    host_dir = Path(__file__).resolve().parent.parent / "attempts" / "execution_host"
    source_files = []
    for relative_path in (
        "production_host.py",
        "production_worker.py",
        "host_protocol.py",
        "host_receipts.py",
    ):
        path = host_dir / relative_path
        source_files.append(
            {
                "path": f"graph_engine/attempts/execution_host/{relative_path}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    projection: JSONValue = {
        "implementation_id": TASK_HOST_IMPLEMENTATION_ID,
        "source_files": source_files,
        "wire_schema_version": TASK_HOST_WIRE_SCHEMA_VERSION,
    }
    return ExecutionHostLock(
        implementation_id=TASK_HOST_IMPLEMENTATION_ID,
        implementation_digest=canonical_digest(projection),
        wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
    )


class ProductLock(FrozenModel):
    schema_version: Literal["3"] = "3"
    digest_algorithm: Literal["graph-engine-source-v1"] = "graph-engine-source-v1"
    engine_api: str
    engine: LockedSource
    engine_digest: str
    product: LockedProduct
    plugins: tuple[LockedPlugin, ...]
    dependency_order: tuple[str, ...]
    registry_projections: RegistryProjections
    registry_digests: RegistryDigests
    configuration: FrozenJSONValue
    configuration_digest: str
    capability_bindings: FrozenJSONValue
    capability_bindings_digest: str
    canonical_bytes: bytes = Field(exclude=True, repr=False)
    digest: str

    @field_validator(
        "engine_digest",
        "configuration_digest",
        "capability_bindings_digest",
        "digest",
    )
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _sha256(value, "product lock")

    @model_validator(mode="after")
    def _authenticate_canonical_form(self) -> ProductLock:
        expected = canonical_json_bytes(_product_lock_projection(self))
        if self.canonical_bytes != expected:
            raise ValueError("product lock canonical bytes disagree with its projection")
        if hashlib.sha256(expected).hexdigest() != self.digest:
            raise ValueError("product lock digest does not authenticate canonical bytes")
        if self.engine.kind != SourceKind.ENGINE or self.engine.digest != self.engine_digest:
            raise ValueError("product lock engine identity disagrees with its digest")
        engine_identity = thaw_json(self.engine.identity)
        if not isinstance(engine_identity, dict) or (
            engine_identity.get("distribution") != "graph-engine"
            or not isinstance(engine_identity.get("version"), str)
            or engine_identity.get("installation") not in {"installed", "editable"}
        ):
            raise ValueError("product lock engine identity is incomplete")
        if engine_identity["installation"] == "installed" and "root" in engine_identity:
            raise ValueError("installed engine identity must be relocatable")
        if engine_identity["installation"] == "editable" and not isinstance(engine_identity.get("root"), str):
            raise ValueError("editable engine identity must retain its root")
        plugin_ids = tuple(plugin.plugin_id for plugin in self.plugins)
        if plugin_ids != tuple(sorted(plugin_ids)) or len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("locked plugins must have unique canonical order")
        if len(self.dependency_order) != len(set(self.dependency_order)):
            raise ValueError("product lock dependency order must not repeat plugin ids")
        if set(self.dependency_order) != set(plugin_ids):
            raise ValueError("product lock dependency order disagrees with locked plugins")
        try:
            manifest = ProductManifest.model_validate(thaw_json(self.product.manifest))
            expected_order = resolve_dependency_order(
                {plugin.plugin_id: plugin.descriptor for plugin in self.plugins},
                manifest.plugins,
            )
        except (ValueError, TypeError, DependencyConflict) as error:
            raise ValueError("product lock dependency declarations are invalid") from error
        if self.dependency_order != expected_order:
            raise ValueError("product lock dependency order is not canonical")
        _validate_locked_contribution_projection_set(self.plugins, self.registry_projections)
        expected_registry_digests = _registry_digests_from_projections(self.registry_projections)
        if self.registry_digests != expected_registry_digests:
            raise ValueError("product lock registry digests do not authenticate their projections")
        configuration = cast(JSONValue, thaw_json(self.configuration))
        if canonical_digest(configuration) != self.configuration_digest:
            raise ValueError("product lock configuration digest does not authenticate its projection")
        bindings = cast(JSONValue, thaw_json(self.capability_bindings))
        if not isinstance(bindings, list):
            raise ValueError("product lock capability bindings must be a list")
        expected_bindings = _binding_projection_from_capabilities(self.registry_projections.capabilities)
        if bindings != expected_bindings:
            raise ValueError("product lock capability bindings disagree with the capability registry")
        if canonical_digest(bindings) != self.capability_bindings_digest:
            raise ValueError("product lock binding digest does not authenticate its projection")
        return self

    @classmethod
    def create(
        cls,
        *,
        engine_api: str,
        engine: LockedSource,
        engine_digest: str,
        product: LockedProduct,
        plugins: tuple[LockedPlugin, ...],
        dependency_order: tuple[str, ...],
        registry_projections: RegistryProjections,
        registry_digests: RegistryDigests,
        configuration: object,
        configuration_digest: str,
        capability_bindings: object,
        capability_bindings_digest: str,
    ) -> Self:
        ordered_plugins = tuple(sorted(plugins, key=lambda plugin: plugin.plugin_id))
        values = {
            "schema_version": "3",
            "digest_algorithm": "graph-engine-source-v1",
            "engine_api": engine_api,
            "engine": engine,
            "engine_digest": engine_digest,
            "product": product,
            "plugins": ordered_plugins,
            "dependency_order": tuple(dependency_order),
            "registry_projections": registry_projections,
            "registry_digests": registry_digests,
            "configuration": configuration,
            "configuration_digest": configuration_digest,
            "capability_bindings": capability_bindings,
            "capability_bindings_digest": capability_bindings_digest,
        }
        projection = _product_lock_values_projection(**values)
        encoded = canonical_json_bytes(projection)
        return cls(
            **values,
            canonical_bytes=encoded,
            digest=hashlib.sha256(encoded).hexdigest(),
        )


def build_product_lock(
    *,
    manifest: ProductManifest,
    product_snapshot: SourceSnapshot,
    descriptors: Mapping[str, PluginDescriptor],
    dependency_order: tuple[str, ...],
    registries: RegistrySet,
    configuration: object,
    engine_snapshot: SourceSnapshot,
    contribution_authorities: Mapping[str, ContributionAuthority],
) -> ProductLock:
    validate_registry_contribution_authorities(
        registries,
        contribution_authorities,
        tuple(descriptors[plugin_id] for plugin_id in dependency_order),
    )
    manifest_projection = _manifest_projection(manifest)
    locked_product = LockedProduct(
        product_id=manifest.product_id,
        product_version=manifest.product_version,
        manifest=manifest_projection,
        manifest_digest=canonical_digest(manifest_projection),
        source=_locked_source(product_snapshot),
    )
    locked_plugins: list[LockedPlugin] = []
    for plugin_id in sorted(descriptors):
        descriptor = descriptors[plugin_id]
        role = SourceRole.PLUGIN if descriptor.source is not None else SourceRole.CONFIG
        source_entry = registries.sources.entries.get(SourceKey(role, plugin_id))
        if source_entry is None:
            raise ValueError(f"locked plugin has no selected source: {plugin_id}")
        descriptor_projection = _descriptor_projection(descriptor)
        try:
            authority = contribution_authorities[plugin_id]
        except KeyError as error:
            raise ValueError(f"locked plugin has no contribution authority: {plugin_id}") from error
        contribution_projection = cast(JSONValue, thaw_json(authority.projection))
        locked_plugins.append(
            LockedPlugin(
                plugin_id=plugin_id,
                plugin_version=descriptor.plugin_version,
                descriptor=descriptor,
                descriptor_digest=canonical_digest(descriptor_projection),
                contribution=contribution_projection,
                contribution_digest=canonical_digest(contribution_projection),
                dependencies=tuple(
                    LockedDependency(
                        plugin_id=dependency.plugin_id,
                        version_specifier=dependency.version_specifier,
                    )
                    for dependency in sorted(
                        descriptor.dependencies,
                        key=lambda dependency: dependency.plugin_id,
                    )
                ),
                source=_locked_source(source_entry.snapshot),
            )
        )
    configuration_projection = cast(JSONValue, thaw_json(configuration))
    registry_projections = compute_registry_projections(registries)
    capability_bindings = _binding_projection_from_capabilities(registry_projections.capabilities)
    return ProductLock.create(
        engine_api=ENGINE_API_VERSION,
        engine=_locked_source(engine_snapshot),
        engine_digest=engine_snapshot.digest,
        product=locked_product,
        plugins=tuple(locked_plugins),
        dependency_order=dependency_order,
        registry_projections=registry_projections,
        registry_digests=_registry_digests_from_projections(registry_projections),
        configuration=configuration_projection,
        configuration_digest=canonical_digest(configuration_projection),
        capability_bindings=capability_bindings,
        capability_bindings_digest=canonical_digest(capability_bindings),
    )


def compute_registry_projections(registries: RegistrySet) -> RegistryProjections:
    sources: list[JSONValue] = []
    for source_key, entry in registries.sources.entries.items():
        source = _locked_source(entry.snapshot)
        sources.append(
            {
                "source_key": {
                    "role": source_key.role.value,
                    "owner_id": source_key.owner_id,
                },
                "kind": source.kind.value,
                "identity": cast(JSONValue, thaw_json(source.identity)),
                "digest": source.digest,
                "files": [
                    {"path": source_file.path, "sha256": source_file.sha256} for source_file in source.files
                ],
            }
        )

    capabilities: list[JSONValue] = []
    for capability_id, entry in registries.capabilities.entries.items():
        base: dict[str, JSONValue] = {
            "capability_id": capability_id,
            "owner_id": entry.owner_id,
        }
        if isinstance(entry, TaskHandlerEntry):
            base["kind"] = "task_handler"
            base["implementation"] = entry.provenance.projection()
            base["implementation_digest"] = entry.provenance.digest
        elif isinstance(entry, CommitValidatorEntry):
            base["kind"] = "commit_validator"
            base["implementation"] = entry.provenance.projection()
            base["implementation_digest"] = entry.provenance.digest
        elif isinstance(entry, CapabilityBindingEntry):
            base.update(
                {
                    "kind": "binding",
                    "target_capability_id": entry.target_capability_id,
                    "data": cast(JSONValue, thaw_json(entry.data)),
                    "resource_ids": list(entry.resource_ids),
                    "secret_handles": list(entry.secret_handles),
                    "target_implementation": entry.target_provenance.projection(),
                    "implementation_digest": entry.target_provenance.digest,
                }
            )
            if entry.contract_id is not None:
                base["contract_id"] = entry.contract_id
        else:  # pragma: no cover - capability registry is a closed authenticated union.
            raise TypeError(f"unsupported capability entry: {type(entry).__name__}")
        capabilities.append(base)

    schemas: list[JSONValue] = [
        {
            "schema_id": schema_id,
            "owner_id": entry.owner_id,
            "media_type": entry.media_type,
            "sha256": entry.sha256,
            "dialect": entry.dialect,
        }
        for schema_id, entry in registries.schemas.entries.items()
    ]
    resources: list[JSONValue] = [
        {
            "resource_id": resource_id,
            "owner_id": entry.owner_id,
            "media_type": entry.media_type,
            "sha256": entry.sha256,
        }
        for resource_id, entry in registries.resources.entries.items()
    ]
    return RegistryProjections(
        sources=sources,
        capabilities=capabilities,
        schemas=schemas,
        resources=resources,
        attempt_contracts=[],
    )


def compute_registry_digests(registries: RegistrySet) -> RegistryDigests:
    return _registry_digests_from_projections(compute_registry_projections(registries))


def _validate_locked_contribution_projection_set(
    plugins: tuple[LockedPlugin, ...],
    projections: RegistryProjections,
) -> None:
    capabilities = _projection_entries(projections.capabilities, "capabilities")
    schemas = _projection_entries(projections.schemas, "schemas")
    resources = _projection_entries(projections.resources, "resources")
    plugin_ids = {plugin.plugin_id for plugin in plugins}
    actual_owners: set[str] = set()
    for group in (capabilities, schemas, resources):
        for entry in group:
            owner = entry.get("owner_id")
            if not isinstance(owner, str):
                raise ValueError("invocation lock registry contribution owner must be text")
            actual_owners.add(owner)
    if not actual_owners.issubset(plugin_ids):
        raise ValueError("invocation lock registries contain an unselected contribution owner")
    contributions: list[ContributionProjection] = []
    for plugin in plugins:
        owner_id = plugin.plugin_id
        contribution = ContributionProjection.model_validate(thaw_json(plugin.contribution))
        expected_role = (
            SourceRole.CONFIG if plugin.source.kind is SourceKind.CONFIG_TREE else SourceRole.PLUGIN
        )
        contribution.validate_selected(
            plugin.descriptor,
            SourceKey(expected_role, owner_id),
            plugin.source.digest,
        )
        contributions.append(contribution)
        actual_capabilities: list[JSONValue] = []
        for entry in capabilities:
            if entry.get("owner_id") != owner_id:
                continue
            if entry.get("kind") == "binding":
                entry = {
                    key: value
                    for key, value in entry.items()
                    if key not in {"target_implementation", "implementation_digest"}
                }
            actual_capabilities.append(cast(JSONValue, entry))
        if actual_capabilities != contribution.capability_registry_projection():
            raise ValueError("invocation lock capabilities disagree with contribution authority")
        actual_schemas = [entry for entry in schemas if entry.get("owner_id") == owner_id]
        if actual_schemas != contribution.schema_registry_projection():
            raise ValueError("invocation lock schemas disagree with contribution authority")
        actual_resources = [entry for entry in resources if entry.get("owner_id") == owner_id]
        if actual_resources != contribution.resource_registry_projection():
            raise ValueError("invocation lock resources disagree with contribution authority")
    validate_contribution_projection_set(tuple(contributions))


def _projection_entries(value: FrozenJSONValue, kind: str) -> list[dict[str, JSONValue]]:
    projection = thaw_json(value)
    if not isinstance(projection, list) or any(not isinstance(entry, dict) for entry in projection):
        raise ValueError(f"invocation lock {kind} projection must be a list of mappings")
    return cast(list[dict[str, JSONValue]], projection)


def authenticate_composition_lock(
    manifest: ProductManifest,
    descriptors: tuple[PluginDescriptor, ...],
    registries: RegistrySet,
    configuration: object,
    lock: ProductLock,
    contribution_authorities: Mapping[str, ContributionAuthority],
) -> None:
    if not isinstance(lock, ProductLock):
        raise TypeError("composition lock must be a ProductLock")
    validate_registry_contribution_authorities(
        registries,
        contribution_authorities,
        descriptors,
    )
    engine_source = registries.sources.entries.get(SourceKey(SourceRole.ENGINE, "graph.engine"))
    if engine_source is None or lock.engine != _locked_source(engine_source.snapshot):
        raise ValueError("invocation lock engine source disagrees with composition")
    if lock.product.product_id != manifest.product_id or (
        lock.product.product_version != manifest.product_version
    ):
        raise ValueError("invocation lock product disagrees with composition manifest")
    product_source = registries.sources.entries.get(SourceKey(SourceRole.PRODUCT, manifest.product_id))
    if product_source is None or lock.product.source != _locked_source(product_source.snapshot):
        raise ValueError("invocation lock product source disagrees with composition")
    manifest_projection = _manifest_projection(manifest)
    if lock.product.manifest_digest != canonical_digest(manifest_projection):
        raise ValueError("invocation lock manifest digest disagrees with composition")
    descriptor_by_id = {descriptor.plugin_id: descriptor for descriptor in descriptors}
    locked_plugin_by_id = {plugin.plugin_id: plugin for plugin in lock.plugins}
    if len(descriptor_by_id) != len(descriptors) or set(descriptor_by_id) != set(locked_plugin_by_id):
        raise ValueError("invocation lock plugins disagree with composition descriptors")
    for plugin_id, descriptor in descriptor_by_id.items():
        locked_plugin = locked_plugin_by_id[plugin_id]
        if (
            locked_plugin.plugin_version != str(Version(descriptor.plugin_version))
            or locked_plugin.descriptor != descriptor
            or locked_plugin.descriptor_digest != canonical_digest(_descriptor_projection(descriptor))
        ):
            raise ValueError(f"invocation lock descriptor disagrees with composition: {plugin_id}")
        authority = contribution_authorities[plugin_id]
        contribution_projection = cast(JSONValue, thaw_json(authority.projection))
        if thaw_json(
            locked_plugin.contribution
        ) != contribution_projection or locked_plugin.contribution_digest != canonical_digest(
            contribution_projection
        ):
            raise ValueError(
                f"invocation lock contribution authority disagrees with composition: {plugin_id}"
            )
        role = SourceRole.PLUGIN if descriptor.source is not None else SourceRole.CONFIG
        plugin_source = registries.sources.entries.get(SourceKey(role, plugin_id))
        if plugin_source is None or locked_plugin.source != _locked_source(plugin_source.snapshot):
            raise ValueError(f"invocation lock plugin source disagrees with composition: {plugin_id}")
        expected_dependencies = tuple(
            (dependency.plugin_id, str(SpecifierSet(dependency.version_specifier)))
            for dependency in sorted(
                descriptor.dependencies,
                key=lambda dependency: dependency.plugin_id,
            )
        )
        locked_dependencies = tuple(
            (dependency.plugin_id, dependency.version_specifier) for dependency in locked_plugin.dependencies
        )
        if locked_dependencies != expected_dependencies:
            raise ValueError(f"invocation lock dependencies disagree with composition: {plugin_id}")
    try:
        expected_order = resolve_dependency_order(descriptor_by_id, manifest.plugins)
    except DependencyConflict as error:
        raise ValueError("composition dependency declarations are invalid") from error
    if lock.dependency_order != expected_order:
        raise ValueError("invocation lock dependency order disagrees with canonical composition order")
    registry_projections = compute_registry_projections(registries)
    if lock.registry_projections != registry_projections:
        raise ValueError("invocation lock registry projections disagree with composition")
    if lock.registry_digests != _registry_digests_from_projections(registry_projections):
        raise ValueError("invocation lock registry digests disagree with composition")
    configuration_projection = cast(JSONValue, thaw_json(configuration))
    if thaw_json(lock.configuration) != configuration_projection:
        raise ValueError("invocation lock configuration projection disagrees with composition")
    if lock.configuration_digest != canonical_digest(configuration_projection):
        raise ValueError("invocation lock configuration digest disagrees with composition")
    capability_bindings = _binding_projection_from_capabilities(registry_projections.capabilities)
    if thaw_json(lock.capability_bindings) != capability_bindings:
        raise ValueError("invocation lock capability bindings disagree with composition")
    if lock.capability_bindings_digest != canonical_digest(capability_bindings):
        raise ValueError("invocation lock capability binding digest disagrees with composition")


def _locked_source(snapshot: SourceSnapshot) -> LockedSource:
    return LockedSource(
        kind=snapshot.identity.kind,
        identity=_source_identity_projection(snapshot.identity),
        digest=snapshot.digest,
        files=tuple(
            LockedSourceFile(path=source_file.path, sha256=source_file.sha256)
            for source_file in snapshot.files
        ),
    )


def _source_identity_projection(identity: SourceIdentity) -> dict[str, JSONValue]:
    projection: dict[str, JSONValue] = {}
    if identity.kind in {
        SourceKind.EDITABLE_PRODUCT,
        SourceKind.EDITABLE_PLUGIN,
        SourceKind.CONFIG_TREE,
    } or (identity.kind == SourceKind.ENGINE and identity.engine_installation == "editable"):
        projection["root"] = str(identity.root)
    for field_name in (
        "distribution",
        "version",
        "entrypoint_group",
        "entrypoint_name",
        "entrypoint_value",
        "product_id",
        "product_version",
        "plugin_id",
        "plugin_version",
        "declaration_path",
    ):
        value = getattr(identity, field_name)
        if value is not None:
            projection[field_name] = value
    if identity.import_roots is not None:
        projection["import_roots"] = list(identity.import_roots)
    if identity.engine_installation is not None:
        projection["installation"] = identity.engine_installation
    return projection


def _manifest_projection(manifest: ProductManifest) -> JSONValue:
    return cast(JSONValue, manifest.model_dump(mode="json", by_alias=True))


def _descriptor_projection(descriptor: PluginDescriptor) -> JSONValue:
    return cast(
        JSONValue,
        {
            "schema_version": descriptor.schema_version,
            "source": (descriptor.source.model_dump(mode="json") if descriptor.source is not None else None),
            "plugin_id": descriptor.plugin_id,
            "plugin_version": str(Version(descriptor.plugin_version)),
            "engine_api": _normalized_engine_api(descriptor.engine_api),
            "dependencies": [
                {
                    "plugin_id": dependency.plugin_id,
                    "version_specifier": str(SpecifierSet(dependency.version_specifier)),
                }
                for dependency in sorted(
                    descriptor.dependencies,
                    key=lambda dependency: dependency.plugin_id,
                )
            ],
            "task_handlers": sorted(descriptor.task_handlers),
            "commit_validators": sorted(descriptor.commit_validators),
            "schemas": sorted(descriptor.schemas),
            "resources": sorted(descriptor.resources),
            "bindings": sorted(descriptor.bindings),
            "attempt_contracts": [
                {"contract_id": item.contract_id, "digest": item.digest}
                for item in descriptor.attempt_contracts
            ],
        },
    )


def _product_lock_projection(lock: ProductLock) -> JSONValue:
    return _product_lock_values_projection(
        schema_version=lock.schema_version,
        digest_algorithm=lock.digest_algorithm,
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=lock.registry_projections,
        registry_digests=lock.registry_digests,
        configuration=lock.configuration,
        configuration_digest=lock.configuration_digest,
        capability_bindings=lock.capability_bindings,
        capability_bindings_digest=lock.capability_bindings_digest,
    )


def _product_lock_values_projection(
    *,
    schema_version: str,
    digest_algorithm: str,
    engine_api: str,
    engine: LockedSource,
    engine_digest: str,
    product: LockedProduct,
    plugins: tuple[LockedPlugin, ...],
    dependency_order: tuple[str, ...],
    registry_projections: RegistryProjections,
    registry_digests: RegistryDigests,
    configuration: object,
    configuration_digest: str,
    capability_bindings: object,
    capability_bindings_digest: str,
) -> JSONValue:
    return {
        "schema_version": schema_version,
        "digest_algorithm": digest_algorithm,
        "engine_api": engine_api,
        "engine": _locked_source_projection(engine),
        "engine_digest": engine_digest,
        "product": _locked_product_projection(product),
        "plugins": [_locked_plugin_projection(plugin) for plugin in plugins],
        "dependency_order": list(dependency_order),
        "registry_projections": _registry_projection_map(registry_projections),
        "registry_digests": _registry_digest_map(registry_digests),
        "configuration": cast(JSONValue, thaw_json(configuration)),
        "configuration_digest": configuration_digest,
        "capability_bindings": cast(JSONValue, thaw_json(capability_bindings)),
        "capability_bindings_digest": capability_bindings_digest,
    }


def _registry_projection_map(projections: RegistryProjections) -> dict[str, JSONValue]:
    return {
        "sources": cast(JSONValue, thaw_json(projections.sources)),
        "capabilities": cast(JSONValue, thaw_json(projections.capabilities)),
        "schemas": cast(JSONValue, thaw_json(projections.schemas)),
        "resources": cast(JSONValue, thaw_json(projections.resources)),
        "attempt_contracts": cast(JSONValue, thaw_json(projections.attempt_contracts)),
    }


def _registry_digest_map(registry_digests: RegistryDigests) -> dict[str, JSONValue]:
    return {
        "sources": registry_digests.sources,
        "capabilities": registry_digests.capabilities,
        "schemas": registry_digests.schemas,
        "resources": registry_digests.resources,
        "attempt_contracts": registry_digests.attempt_contracts,
    }


def _registry_digests_from_projections(
    projections: RegistryProjections,
) -> RegistryDigests:
    contracts = cast(JSONValue, thaw_json(projections.attempt_contracts))
    return RegistryDigests(
        sources=canonical_digest(cast(JSONValue, thaw_json(projections.sources))),
        capabilities=canonical_digest(cast(JSONValue, thaw_json(projections.capabilities))),
        schemas=canonical_digest(cast(JSONValue, thaw_json(projections.schemas))),
        resources=canonical_digest(cast(JSONValue, thaw_json(projections.resources))),
        attempt_contracts=canonical_digest(contracts),
    )


def _binding_projection_from_capabilities(capabilities: object) -> list[JSONValue]:
    projection = thaw_json(capabilities)
    if not isinstance(projection, list):
        raise ValueError("capability registry projection must be a list")
    bindings: list[JSONValue] = []
    for item in projection:
        if not isinstance(item, dict):
            raise ValueError("capability registry entries must be mappings")
        if item.get("kind") == "binding":
            bindings.append(cast(JSONValue, dict(item)))
    return bindings


def _locked_product_projection(product: LockedProduct) -> JSONValue:
    return {
        "product_id": product.product_id,
        "product_version": product.product_version,
        "manifest": cast(JSONValue, thaw_json(product.manifest)),
        "manifest_digest": product.manifest_digest,
        "source": _locked_source_projection(product.source),
    }


def _locked_plugin_projection(plugin: LockedPlugin) -> JSONValue:
    return {
        "plugin_id": plugin.plugin_id,
        "plugin_version": plugin.plugin_version,
        "descriptor": _descriptor_projection(plugin.descriptor),
        "descriptor_digest": plugin.descriptor_digest,
        "contribution": cast(JSONValue, thaw_json(plugin.contribution)),
        "contribution_digest": plugin.contribution_digest,
        "dependencies": [
            {
                "plugin_id": dependency.plugin_id,
                "version_specifier": dependency.version_specifier,
            }
            for dependency in plugin.dependencies
        ],
        "source": _locked_source_projection(plugin.source),
    }


def _locked_source_projection(source: LockedSource) -> JSONValue:
    return {
        "kind": source.kind.value,
        "identity": cast(JSONValue, thaw_json(source.identity)),
        "digest": source.digest,
        "files": [{"path": source_file.path, "sha256": source_file.sha256} for source_file in source.files],
    }


def _sha256(value: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{kind} digest must be a lowercase SHA-256 hex value")
    return value


def _qualified_id(value: str, kind: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _version(value: str, kind: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _normalized_engine_api(value: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion:
        return str(SpecifierSet(value))


__all__ = [
    "ExecutionHostLock",
    "LockedDependency",
    "LockedPlugin",
    "LockedProduct",
    "LockedSource",
    "LockedSourceFile",
    "ProductLock",
    "RegistryDigests",
    "RegistryProjections",
    "TASK_HOST_IMPLEMENTATION_ID",
    "TASK_HOST_WIRE_SCHEMA_VERSION",
    "compute_registry_digests",
    "compute_registry_projections",
    "pinned_execution_host_lock",
]
