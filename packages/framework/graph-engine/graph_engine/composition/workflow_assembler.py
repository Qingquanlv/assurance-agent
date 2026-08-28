from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graph_engine.canonical import JSONValue
from graph_engine.composition.models import ProductManifest, RegistrySet, ResourceEntry
from graph_engine.errors import GraphEngineError
from graph_engine.graph.module_schema import WorkflowModuleDef, parse_workflow_module
from graph_engine.plugin_api import PluginDescriptor

WORKFLOW_MODULE_MEDIA_TYPE = "application/vnd.graph-engine.workflow-module+yaml"


class WorkflowAssemblyError(GraphEngineError):
    """Raised when authenticated module resources cannot form a closed workflow."""


@dataclass(frozen=True, slots=True)
class LoadedModule:
    module_id: str
    owner_id: str
    resource_id: str
    module_version: str
    media_type: str
    content: bytes
    sha256: str
    module: WorkflowModuleDef


@dataclass(frozen=True, slots=True)
class LoadedModules:
    modules: tuple[LoadedModule, ...]

    def canonical_projection(self) -> JSONValue:
        return [
            {
                "media_type": item.media_type,
                "module_id": item.module_id,
                "module_version": item.module_version,
                "owner_id": item.owner_id,
                "resource_id": item.resource_id,
                "sha256": item.sha256,
            }
            for item in self.modules
        ]


def _selected_descriptors(
    descriptors: Mapping[str, PluginDescriptor] | Sequence[PluginDescriptor],
) -> dict[str, PluginDescriptor]:
    if isinstance(descriptors, Mapping):
        selected = dict(descriptors)
        for plugin_id, descriptor in selected.items():
            if not isinstance(descriptor, PluginDescriptor):
                raise WorkflowAssemblyError("descriptors must contain PluginDescriptor values")
            if descriptor.plugin_id != plugin_id:
                raise WorkflowAssemblyError(
                    f"descriptor owner mismatch: {plugin_id} != {descriptor.plugin_id}"
                )
        return selected
    selected: dict[str, PluginDescriptor] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, PluginDescriptor):
            raise WorkflowAssemblyError("descriptors must contain PluginDescriptor values")
        if descriptor.plugin_id in selected:
            raise WorkflowAssemblyError(f"duplicate descriptor: {descriptor.plugin_id}")
        selected[descriptor.plugin_id] = descriptor
    return selected


def _parse_module(entry: ResourceEntry) -> WorkflowModuleDef:
    try:
        return parse_workflow_module(entry.content)
    except (TypeError, ValueError) as error:
        raise WorkflowAssemblyError(
            f"module resource is not an authenticated workflow module: {entry.resource_id}"
        ) from error


def _load_authenticated_modules(
    *,
    manifest: ProductManifest,
    descriptors: Mapping[str, PluginDescriptor] | Sequence[PluginDescriptor],
    registries: RegistrySet,
) -> LoadedModules:
    if manifest.workflow_module is None:
        raise WorkflowAssemblyError("workflow module resources require the modular product form")
    selected = _selected_descriptors(descriptors)
    selected_owned = tuple(
        entry
        for entry in registries.resources.entries.values()
        if entry.media_type == WORKFLOW_MODULE_MEDIA_TYPE and entry.owner_id in selected
    )

    identities: dict[tuple[str, str], str] = {}
    parsed: dict[str, WorkflowModuleDef] = {}
    for entry in selected_owned:
        module = _parse_module(entry)
        identity = (module.module_id, module.owner_id)
        if identity in identities:
            raise WorkflowAssemblyError(f"duplicate module identity: {module.module_id}")
        identities[identity] = entry.resource_id
        parsed[entry.resource_id] = module

    required_ids = {item.resource_id for item in manifest.workflow_module_resources}
    extra = sorted({entry.resource_id for entry in selected_owned} - required_ids)
    if extra:
        raise WorkflowAssemblyError(f"undeclared extra module resource: {extra[0]}")

    loaded: list[LoadedModule] = []
    for requirement in manifest.workflow_module_resources:
        entry = registries.resources.entries.get(requirement.resource_id)
        if entry is None:
            raise WorkflowAssemblyError(f"missing required resource: {requirement.resource_id}")
        if entry.media_type != WORKFLOW_MODULE_MEDIA_TYPE:
            raise WorkflowAssemblyError(
                f"resource {requirement.resource_id} has wrong media type: {entry.media_type}"
            )
        if entry.owner_id != requirement.owner_id:
            raise WorkflowAssemblyError(f"resource owner mismatch: {requirement.resource_id}")
        descriptor = selected.get(requirement.owner_id)
        if descriptor is None or descriptor.plugin_id != entry.owner_id:
            raise WorkflowAssemblyError(f"resource owner mismatch: {requirement.resource_id}")
        module = parsed.get(entry.resource_id)
        if module is None:
            module = _parse_module(entry)
        if module.module_id != requirement.module_id:
            raise WorkflowAssemblyError(
                f"module ID mismatch: {requirement.resource_id} is {module.module_id}"
            )
        if module.owner_id != requirement.owner_id or module.owner_id != descriptor.plugin_id:
            raise WorkflowAssemblyError(f"resource owner mismatch: {requirement.resource_id}")
        if module.module_version != descriptor.plugin_version:
            raise WorkflowAssemblyError(
                f"module version versus selected descriptor mismatch: {requirement.resource_id}"
            )
        content = bytes(entry.content)
        loaded.append(
            LoadedModule(
                module_id=module.module_id,
                owner_id=module.owner_id,
                resource_id=entry.resource_id,
                module_version=module.module_version,
                media_type=entry.media_type,
                content=content,
                sha256=entry.sha256,
                module=module,
            )
        )
    loaded.sort(key=lambda item: (item.module_id, item.owner_id, item.resource_id))
    return LoadedModules(modules=tuple(loaded))
