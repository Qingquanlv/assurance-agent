from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import re
from types import MappingProxyType
from typing import Protocol, cast

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.models import PluginRequirement, ProductManifest
from graph_engine.composition.contributions import (
    ContributionValueError,
    validate_contribution_values,
)
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import freeze_json
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CommitValidator,
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
    RegistryPorts,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)


class ProductResolutionError(GraphEngineError):
    """Raised when an explicit product bundle cannot be resolved exactly."""


class ProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...


_ENTRYPOINT_NAME = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$|^[a-z][a-z0-9-]*-[a-z0-9-]+$")


@dataclass(frozen=True, slots=True)
class LegacyResolvedCapabilityView:
    """Temporary honest Phase 1 runtime view; Task 8 removes it with the old resolver."""

    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, CommitValidator]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_handlers",
            MappingProxyType(dict(sorted(self.task_handlers.items()))),
        )
        object.__setattr__(
            self,
            "commit_validators",
            MappingProxyType(dict(sorted(self.commit_validators.items()))),
        )


@dataclass(frozen=True, slots=True)
class ResolvedProduct:
    manifest: ProductManifest
    descriptors: tuple[PluginDescriptor, ...]
    registry: LegacyResolvedCapabilityView
    workflow: CompiledWorkflow
    digest: str


@dataclass(frozen=True, slots=True)
class _SelectedPluginProvider:
    provider: PluginProvider
    selected_descriptor: PluginDescriptor

    def descriptor(self) -> PluginDescriptor:
        return self.selected_descriptor

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        return self.provider.contribute(ports)


@dataclass(frozen=True, slots=True)
class _LegacyBoundTaskHandler:
    alias_id: str
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...]
    target: TaskHandler

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        if request.capability_id != self.alias_id:
            raise ValueError(
                f"legacy bound handler for {self.alias_id} received request for {request.capability_id}"
            )
        bound = request.model_copy(
            update={
                "target_capability_id": self.target_capability_id,
                "binding_data": self.data,
                "resource_ids": self.resource_ids,
            }
        )
        return await self.target.execute(bound, context)


def _select_plugin(
    requirement: PluginRequirement,
    available_plugins: Mapping[str, PluginProvider],
) -> _SelectedPluginProvider:
    try:
        provider = available_plugins[requirement.plugin_id]
    except KeyError as error:
        raise ProductResolutionError(
            f"missing plugin {requirement.plugin_id}{requirement.version_specifier}"
        ) from error

    descriptor = provider.descriptor()
    if descriptor.plugin_id != requirement.plugin_id:
        raise ProductResolutionError(
            f"plugin provider for {requirement.plugin_id} described {descriptor.plugin_id}"
        )
    if Version(descriptor.plugin_version) not in SpecifierSet(requirement.version_specifier):
        raise ProductResolutionError(
            f"plugin {requirement.plugin_id} version {descriptor.plugin_version!r}; "
            f"expected {requirement.version_specifier!r}"
        )
    if descriptor.engine_api != ENGINE_API_VERSION:
        raise ProductResolutionError(
            f"plugin {requirement.plugin_id} requires engine API {descriptor.engine_api!r}; "
            f"expected {ENGINE_API_VERSION!r}"
        )
    snapshot = PluginDescriptor(
        schema_version=descriptor.schema_version,
        source=descriptor.source,
        plugin_id=descriptor.plugin_id,
        plugin_version=descriptor.plugin_version,
        engine_api=descriptor.engine_api,
        task_handlers=tuple(descriptor.task_handlers),
        commit_validators=tuple(descriptor.commit_validators),
        dependencies=tuple(descriptor.dependencies),
        schemas=tuple(descriptor.schemas),
        resources=tuple(descriptor.resources),
        effects=tuple(descriptor.effects),
        bindings=tuple(descriptor.bindings),
    )
    return _SelectedPluginProvider(provider=provider, selected_descriptor=snapshot)


def _descriptor_json(descriptor: PluginDescriptor) -> dict[str, JSONValue]:
    return {
        "plugin_id": descriptor.plugin_id,
        "plugin_version": descriptor.plugin_version,
        "engine_api": descriptor.engine_api,
        "task_handlers": cast(list[JSONValue], sorted(descriptor.task_handlers)),
        "commit_validators": cast(list[JSONValue], sorted(descriptor.commit_validators)),
        "dependencies": cast(
            list[JSONValue],
            [
                {
                    "plugin_id": dependency.plugin_id,
                    "version_specifier": dependency.version_specifier,
                }
                for dependency in sorted(
                    descriptor.dependencies,
                    key=lambda item: item.plugin_id,
                )
            ],
        ),
        "schemas": cast(list[JSONValue], sorted(descriptor.schemas)),
        "resources": cast(list[JSONValue], sorted(descriptor.resources)),
        "effects": cast(list[JSONValue], sorted(descriptor.effects)),
        "bindings": cast(list[JSONValue], sorted(descriptor.bindings)),
    }


def _assemble_selected_contributions(
    selected: tuple[_SelectedPluginProvider, ...],
) -> LegacyResolvedCapabilityView:
    """Temporary Phase 1 product bridge; Task 8 removes this resolver."""

    ports = RegistryPorts(ENGINE_API_VERSION)
    task_handlers: dict[str, TaskHandler] = {}
    commit_validators: dict[str, CommitValidator] = {}
    pending_bindings = []
    contributed = tuple((item.selected_descriptor, item.contribute(ports)) for item in selected)
    try:
        validate_contribution_values(
            tuple(
                (descriptor.plugin_id, descriptor, contribution) for descriptor, contribution in contributed
            )
        )
    except ContributionValueError as error:
        message = (
            str(error)
            .replace("unknown binding resource", "unknown legacy binding resource")
            .replace("unknown target capability for binding", "unknown legacy binding target")
        )
        raise ProductResolutionError(message) from error
    for _descriptor, contribution in contributed:
        task_handlers.update(contribution.task_handlers)
        commit_validators.update(contribution.commit_validators)
        pending_bindings.extend(contribution.bindings)
    for binding in pending_bindings:
        target = task_handlers[binding.target_capability_id]
        task_handlers[binding.capability_id] = _LegacyBoundTaskHandler(
            alias_id=binding.capability_id,
            target_capability_id=binding.target_capability_id,
            data=freeze_json(binding.data),
            resource_ids=tuple(binding.resource_ids),
            target=target,
        )
    return LegacyResolvedCapabilityView(
        task_handlers=task_handlers,
        commit_validators=commit_validators,
    )


def resolve_product(
    provider: ProductProvider,
    available_plugins: Mapping[str, PluginProvider],
) -> ResolvedProduct:
    manifest = provider.manifest()
    if manifest.engine_api != ENGINE_API_VERSION:
        raise ProductResolutionError(
            f"product {manifest.product_id} requires engine API {manifest.engine_api!r}; "
            f"expected {ENGINE_API_VERSION!r}"
        )

    selected = tuple(_select_plugin(requirement, available_plugins) for requirement in manifest.plugins)
    descriptors = tuple(item.selected_descriptor for item in selected)
    registry = _assemble_selected_contributions(selected)
    if manifest.workflow is None:
        raise ProductResolutionError("phase-one product resolver requires an inline workflow")
    workflow = compile_workflow(manifest.workflow, registry)
    digest_payload = cast(
        JSONValue,
        {
            "manifest": manifest.model_dump(mode="json", by_alias=True),
            "selected_descriptors": [
                _descriptor_json(descriptor)
                for descriptor in sorted(descriptors, key=lambda item: item.plugin_id)
            ],
            "registry": {
                "task_handlers": sorted(registry.task_handlers),
                "commit_validators": sorted(registry.commit_validators),
            },
            "compiled_digest": workflow.digest,
        },
    )
    return ResolvedProduct(
        manifest=manifest,
        descriptors=descriptors,
        registry=registry,
        workflow=workflow,
        digest=canonical_digest(digest_payload),
    )


def _load_entrypoint(
    entrypoint_name: str,
    *,
    group: str,
    kind: str,
    required_methods: tuple[str, ...],
) -> object:
    try:
        validate_qualified_id(entrypoint_name)
    except IdentifierError:
        if not _ENTRYPOINT_NAME.fullmatch(entrypoint_name):
            raise ProductResolutionError(f"invalid {kind} id: {entrypoint_name!r}") from None

    matches = tuple(
        entry_point
        for entry_point in metadata.entry_points(group=group)
        if entry_point.name == entrypoint_name
    )
    if not matches:
        raise ProductResolutionError(f"no {kind} entry point for {entrypoint_name}")
    if len(matches) > 1:
        raise ProductResolutionError(f"multiple {kind} entry points for {entrypoint_name}")

    try:
        loaded = next(iter(matches)).load()
    except Exception as error:
        raise ProductResolutionError(f"cannot load {kind} entry point {entrypoint_name}") from error
    if any(not callable(getattr(loaded, method, None)) for method in required_methods):
        methods = " and ".join(required_methods)
        raise ProductResolutionError(
            f"{kind} entry point {entrypoint_name} does not provide callable {methods}"
        )
    return loaded


def load_product_entrypoint(entrypoint_name: str) -> ProductProvider:
    loaded = _load_entrypoint(
        entrypoint_name,
        group="graph_engine.products",
        kind="product",
        required_methods=("manifest",),
    )
    return cast(ProductProvider, loaded)


def load_plugin_entrypoint(entrypoint_name: str) -> PluginProvider:
    loaded = _load_entrypoint(
        entrypoint_name,
        group="graph_engine.plugins",
        kind="plugin",
        required_methods=("descriptor", "contribute"),
    )
    return cast(PluginProvider, loaded)


__all__ = [
    "LegacyResolvedCapabilityView",
    "PluginRequirement",
    "ProductManifest",
    "ProductProvider",
    "ProductResolutionError",
    "ResolvedProduct",
    "load_plugin_entrypoint",
    "load_product_entrypoint",
    "resolve_product",
]
