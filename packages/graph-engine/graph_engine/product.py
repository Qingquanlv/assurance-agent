from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Protocol, cast

from pydantic import field_validator, model_validator

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import FrozenModel, WorkflowDef
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CapabilityRegistry,
    EnginePorts,
    PluginDescriptor,
    PluginProvider,
    PluginRuntime,
    assemble_registry,
)


class ProductResolutionError(GraphEngineError):
    """Raised when an explicit product bundle cannot be resolved exactly."""


class PluginRequirement(FrozenModel):
    plugin_id: str
    version: str

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _qualified_id(value, "plugin id")


class ProductManifest(FrozenModel):
    product_id: str
    product_version: str
    engine_api: str
    plugins: tuple[PluginRequirement, ...]
    workflow: WorkflowDef

    @field_validator("product_id")
    @classmethod
    def _validate_product_id(cls, value: str) -> str:
        return _qualified_id(value, "product id")

    @model_validator(mode="after")
    def _validate_plugins(self) -> ProductManifest:
        if not self.plugins:
            raise ValueError("product manifest must require at least one plugin")
        plugin_ids = tuple(requirement.plugin_id for requirement in self.plugins)
        if len(set(plugin_ids)) != len(plugin_ids):
            raise ValueError("product manifest cannot repeat a plugin id")
        return self


class ProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...


@dataclass(frozen=True, slots=True)
class ResolvedProduct:
    manifest: ProductManifest
    descriptors: tuple[PluginDescriptor, ...]
    registry: CapabilityRegistry
    workflow: CompiledWorkflow
    digest: str


@dataclass(frozen=True, slots=True)
class _SelectedPluginProvider:
    provider: PluginProvider
    selected_descriptor: PluginDescriptor

    def descriptor(self) -> PluginDescriptor:
        return self.selected_descriptor

    def bind(self, ports: EnginePorts) -> PluginRuntime:
        return self.provider.bind(ports)


def _qualified_id(value: str, kind: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _select_plugin(
    requirement: PluginRequirement,
    available_plugins: Mapping[str, PluginProvider],
) -> _SelectedPluginProvider:
    try:
        provider = available_plugins[requirement.plugin_id]
    except KeyError as error:
        raise ProductResolutionError(
            f"missing plugin {requirement.plugin_id}=={requirement.version}"
        ) from error

    descriptor = provider.descriptor()
    if descriptor.plugin_id != requirement.plugin_id:
        raise ProductResolutionError(
            f"plugin provider for {requirement.plugin_id} described {descriptor.plugin_id}"
        )
    if descriptor.plugin_version != requirement.version:
        raise ProductResolutionError(
            f"plugin {requirement.plugin_id} version {descriptor.plugin_version!r}; "
            f"expected {requirement.version!r}"
        )
    if descriptor.engine_api != ENGINE_API_VERSION:
        raise ProductResolutionError(
            f"plugin {requirement.plugin_id} requires engine API {descriptor.engine_api!r}; "
            f"expected {ENGINE_API_VERSION!r}"
        )
    snapshot = PluginDescriptor(
        plugin_id=descriptor.plugin_id,
        plugin_version=descriptor.plugin_version,
        engine_api=descriptor.engine_api,
        task_handlers=tuple(descriptor.task_handlers),
        commit_validators=tuple(descriptor.commit_validators),
    )
    return _SelectedPluginProvider(provider=provider, selected_descriptor=snapshot)


def _descriptor_json(descriptor: PluginDescriptor) -> dict[str, JSONValue]:
    return {
        "plugin_id": descriptor.plugin_id,
        "plugin_version": descriptor.plugin_version,
        "engine_api": descriptor.engine_api,
        "task_handlers": cast(list[JSONValue], sorted(descriptor.task_handlers)),
        "commit_validators": cast(list[JSONValue], sorted(descriptor.commit_validators)),
    }


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
    registry = assemble_registry(selected)
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


def load_product_entrypoint(product_id: str) -> ProductProvider:
    try:
        validate_qualified_id(product_id)
    except IdentifierError as error:
        raise ProductResolutionError(f"invalid product id: {product_id!r}") from error

    matches = tuple(
        entry_point
        for entry_point in metadata.entry_points(group="graph_engine.products")
        if entry_point.name == product_id
    )
    if not matches:
        raise ProductResolutionError(f"no product entry point for {product_id}")
    if len(matches) > 1:
        raise ProductResolutionError(f"multiple product entry points for {product_id}")

    loaded = next(iter(matches)).load()
    if not callable(getattr(loaded, "manifest", None)):
        raise ProductResolutionError(f"product entry point {product_id} does not provide callable manifest")
    return cast(ProductProvider, loaded)


__all__ = [
    "PluginRequirement",
    "ProductManifest",
    "ProductProvider",
    "ProductResolutionError",
    "ResolvedProduct",
    "load_product_entrypoint",
    "resolve_product",
]
