from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from graph_engine.canonical import JSONValue
from graph_engine.composition.models import (
    CapabilityBindingEntry,
    ProductManifest,
    RegistrySet,
    ResourceEntry,
    WorkflowSlotBinding,
)
from graph_engine.errors import GraphEngineError
from graph_engine.graph.module_schema import (
    WorkflowExportDef,
    WorkflowModuleDef,
    parse_workflow_module,
)
from graph_engine.graph.schema import (
    GraphDef,
    NodeDef,
    RetryPolicyDef,
    TimeoutPolicyDef,
    WorkflowDef,
)
from graph_engine.plugin_api import PluginDescriptor

_T = TypeVar("_T")
_STRUCTURAL_NODE_KINDS = frozenset({"subgraph", "gate", "join", "interrupt", "end"})

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


def _qualified_symbol(module_id: str, kind: str, local_id: str) -> str:
    return f"{module_id}.{kind}.{local_id}"


def _sorted_mapping(values: Mapping[str, _T]) -> dict[str, _T]:
    return {key: values[key] for key in sorted(values)}


def _module_records(
    loaded: LoadedModules,
    product: WorkflowModuleDef,
) -> list[tuple[str, str, WorkflowModuleDef]]:
    records = [(product.module_id, product.owner_id, product)]
    seen = {product.module_id}
    for item in loaded.modules:
        if item.module_id in seen:
            raise WorkflowAssemblyError(f"duplicate module identity: {item.module_id}")
        seen.add(item.module_id)
        records.append((item.module_id, item.owner_id, item.module))
    records.sort(key=lambda item: (item[0], item[1]))
    return records


def _reject_duplicate_symbols(module_id: str, module: WorkflowModuleDef) -> None:
    imports = set(module.imports)
    overlap = (imports & set(module.graphs)) | (imports & set(module.exports))
    if overlap:
        name = sorted(overlap)[0]
        raise WorkflowAssemblyError(f"duplicate local/export/import name collision: {name} in {module_id}")


def _reject_module_cycles(import_edges: Mapping[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(module_id: str) -> None:
        if module_id in visited:
            return
        if module_id in visiting:
            raise WorkflowAssemblyError(f"module cycle: {module_id}")
        visiting.add(module_id)
        for target in import_edges.get(module_id, ()):
            walk(target)
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in sorted(import_edges):
        walk(module_id)


def _reject_product_structural_rules(product: WorkflowModuleDef) -> None:
    for graph_id, graph in product.graphs.items():
        for node_id, node in graph.nodes.items():
            location = f"{product.module_id}/{graph_id}/{node_id}"
            if node.kind == "task":
                raise WorkflowAssemblyError(f"product task node is not allowed: {location}")
            if node.capability is not None:
                raise WorkflowAssemblyError(f"product direct capability reference: {location}")
            if node.kind not in _STRUCTURAL_NODE_KINDS:
                raise WorkflowAssemblyError(f"product root has structural nodes only: {location}")
            if node.graph is not None and node.graph not in product.graphs:
                raise WorkflowAssemblyError(f"product direct graph reference: {location}")


def _reject_missing_routing(module_id: str, module: WorkflowModuleDef) -> None:
    for graph_id, graph in module.graphs.items():
        outgoing: dict[str, int] = {node_id: 0 for node_id in graph.nodes}
        for edge in graph.edges:
            outgoing[edge.from_] = outgoing.get(edge.from_, 0) + 1
        for node_id, count in outgoing.items():
            if count > 1 and graph.nodes[node_id].routing is None:
                raise WorkflowAssemblyError(f"modular node {module_id}/{graph_id}/{node_id} requires routing")


def _union_registry_refs(
    modules: Sequence[WorkflowModuleDef],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    schemas: set[str] = set()
    resources: set[str] = set()
    effects: set[str] = set()
    for module in modules:
        schemas.update(module.schemas)
        resources.update(module.resources)
        effects.update(module.effects)
    return tuple(sorted(schemas)), tuple(sorted(resources)), tuple(sorted(effects))


def _reject_unregistered_refs(
    *,
    schemas: Sequence[str],
    resources: Sequence[str],
    effects: Sequence[str],
    extra_schemas: Sequence[str],
    registries: RegistrySet,
) -> None:
    for schema_id in (*schemas, *extra_schemas):
        if schema_id not in registries.schemas.entries:
            raise WorkflowAssemblyError(f"unregistered schema: {schema_id}")
    for resource_id in resources:
        if resource_id not in registries.resources.entries:
            raise WorkflowAssemblyError(f"unregistered resource: {resource_id}")
    for effect_id in effects:
        if effect_id not in registries.effects.entries:
            raise WorkflowAssemblyError(f"unregistered effect: {effect_id}")


def _rewrite_node(
    *,
    module_id: str,
    module: WorkflowModuleDef,
    node: NodeDef,
    graph_names: Mapping[str, str],
    retry_names: Mapping[str, str],
    timeout_names: Mapping[str, str],
    modules_by_id: Mapping[str, WorkflowModuleDef],
    export_table: Mapping[tuple[str, str], tuple[str, WorkflowExportDef]],
) -> NodeDef:
    payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
    if node.retry is not None:
        if node.retry not in retry_names:
            raise WorkflowAssemblyError(f"dangling policy reference: {module_id}/{node.retry}")
        payload["retry"] = retry_names[node.retry]
    if node.timeout is not None:
        if node.timeout not in timeout_names:
            raise WorkflowAssemblyError(f"dangling policy reference: {module_id}/{node.timeout}")
        payload["timeout"] = timeout_names[node.timeout]
    if node.graph_import is not None:
        spec = module.imports.get(node.graph_import)
        if spec is None:
            raise WorkflowAssemblyError(f"undeclared import alias: {node.graph_import}")
        target = modules_by_id.get(spec.module_id)
        if target is None:
            raise WorkflowAssemblyError(f"missing import module: {spec.module_id}")
        if target.owner_id != spec.owner_id:
            raise WorkflowAssemblyError(f"import owner mismatch: {spec.module_id} owner {spec.owner_id}")
        exported = export_table.get((spec.module_id, spec.export))
        if exported is None:
            if spec.export in target.graphs:
                raise WorkflowAssemblyError(f"private graph reference: {spec.module_id}/{spec.export}")
            raise WorkflowAssemblyError(f"missing export: {spec.module_id}/{spec.export}")
        _qualified_graph, export = exported
        payload.pop("graph_import", None)
        payload["graph"] = _qualified_graph
        payload["input_schema"] = export.input_schema
        payload["output_schema"] = export.output_schema
        payload["output_projection"] = export.output_projection.model_dump(
            mode="python",
            exclude_unset=True,
        )
    elif node.graph is not None:
        if node.graph not in graph_names:
            raise WorkflowAssemblyError(f"invalid local symbol: {module_id}/{node.graph}")
        payload["graph"] = graph_names[node.graph]
    if node.capability_slot is not None and node.capability_slot not in module.capability_slots:
        raise WorkflowAssemblyError(f"undeclared capability slot: {module_id}/{node.capability_slot}")
    rewritten = NodeDef.model_validate(payload)
    if rewritten.graph_import is not None:
        raise WorkflowAssemblyError(f"unresolved graph_import: {rewritten.graph_import}")
    return rewritten


def _lower_module_symbols(
    loaded: LoadedModules,
    *,
    manifest: ProductManifest,
    registries: RegistrySet,
) -> WorkflowDef:
    if manifest.workflow_module is None:
        raise WorkflowAssemblyError("workflow module resources require the modular product form")
    product = manifest.workflow_module
    records = _module_records(loaded, product)
    modules_by_id = {module_id: module for module_id, _owner_id, module in records}

    for module_id, _owner_id, module in records:
        if module.role == "feature" and module.entrypoints:
            raise WorkflowAssemblyError(f"feature entrypoint is not allowed: {module_id}")
        _reject_duplicate_symbols(module_id, module)

    import_edges = {
        module_id: tuple(sorted({spec.module_id for spec in module.imports.values()}))
        for module_id, _owner_id, module in records
    }
    _reject_module_cycles(import_edges)

    for module_id, _owner_id, module in records:
        if module.role == "feature" and module.imports:
            raise WorkflowAssemblyError(
                f"feature-to-feature graph import is not allowed: {module_id} import tables must be empty"
            )

    _reject_product_structural_rules(product)
    for module_id, _owner_id, module in records:
        _reject_missing_routing(module_id, module)

    export_table: dict[tuple[str, str], tuple[str, WorkflowExportDef]] = {}
    for module_id, _owner_id, module in records:
        for export_name, export in module.exports.items():
            export_table[(module_id, export_name)] = (
                _qualified_symbol(module_id, "graph", export.graph),
                export,
            )

    declared_schemas, declared_resources, declared_effects = _union_registry_refs(
        [module for _module_id, _owner_id, module in records]
    )
    extra_schemas: list[str] = []
    for _module_id, _owner_id, module in records:
        for export in module.exports.values():
            extra_schemas.append(export.input_schema)
            extra_schemas.append(export.output_schema)
        for graph in module.graphs.values():
            for node in graph.nodes.values():
                if node.input_schema is not None:
                    extra_schemas.append(node.input_schema)
                if node.output_schema is not None:
                    extra_schemas.append(node.output_schema)
    _reject_unregistered_refs(
        schemas=declared_schemas,
        resources=declared_resources,
        effects=declared_effects,
        extra_schemas=extra_schemas,
        registries=registries,
    )

    retry: dict[str, RetryPolicyDef] = {}
    timeout: dict[str, TimeoutPolicyDef] = {}
    graphs: dict[str, GraphDef] = {}
    for module_id, _owner_id, module in records:
        graph_names = {
            local_id: _qualified_symbol(module_id, "graph", local_id) for local_id in module.graphs
        }
        retry_names = {local_id: _qualified_symbol(module_id, "retry", local_id) for local_id in module.retry}
        timeout_names = {
            local_id: _qualified_symbol(module_id, "timeout", local_id) for local_id in module.timeout
        }
        for local_id, policy in module.retry.items():
            retry[retry_names[local_id]] = policy
        for local_id, policy in module.timeout.items():
            timeout[timeout_names[local_id]] = policy
        for local_id, graph in module.graphs.items():
            rewritten_nodes = {
                node_id: _rewrite_node(
                    module_id=module_id,
                    module=module,
                    node=node,
                    graph_names=graph_names,
                    retry_names=retry_names,
                    timeout_names=timeout_names,
                    modules_by_id=modules_by_id,
                    export_table=export_table,
                )
                for node_id, node in graph.nodes.items()
            }
            graphs[graph_names[local_id]] = GraphDef(
                max_activations=graph.max_activations,
                start=graph.start,
                nodes=rewritten_nodes,
                edges=graph.edges,
            )

    if product.name is None or not product.name.strip():
        raise WorkflowAssemblyError("product module requires a non-empty name")
    entrypoints = {
        name: _qualified_symbol(product.module_id, "graph", graph_id)
        for name, graph_id in product.entrypoints.items()
    }
    workflow = WorkflowDef(
        name=product.name,
        entrypoints=entrypoints,
        schemas=declared_schemas,
        resources=declared_resources,
        effects=declared_effects,
        retry=_sorted_mapping(retry),
        timeout=_sorted_mapping(timeout),
        graphs=_sorted_mapping(graphs),
    )
    for graph in workflow.graphs.values():
        for node in graph.nodes.values():
            if node.graph_import is not None:
                raise WorkflowAssemblyError(f"unresolved graph_import: {node.graph_import}")
    return workflow


def _modules_by_id(loaded: LoadedModules, product: WorkflowModuleDef) -> dict[str, WorkflowModuleDef]:
    modules = {product.module_id: product}
    for item in loaded.modules:
        if item.module_id in modules:
            raise WorkflowAssemblyError(f"duplicate module identity: {item.module_id}")
        modules[item.module_id] = item.module
    return modules


def _selected_slot_contracts(
    modules: Mapping[str, WorkflowModuleDef],
) -> dict[tuple[str, str], str]:
    slots: dict[tuple[str, str], str] = {}
    for module_id, module in modules.items():
        for slot_name, slot in module.capability_slots.items():
            key = (module_id, slot_name)
            if key in slots:
                raise WorkflowAssemblyError(f"duplicate capability slot: {module_id}/{slot_name}")
            slots[key] = slot.contract_id
    return slots


def _module_id_for_graph(graph_id: str, module_ids: Mapping[str, WorkflowModuleDef]) -> str:
    matches = [module_id for module_id in module_ids if graph_id.startswith(f"{module_id}.graph.")]
    if not matches:
        raise WorkflowAssemblyError(f"unknown assembled graph: {graph_id}")
    return max(matches, key=len)


def _nested_payload_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        keys.update(value)
        for item in value.values():
            keys.update(_nested_payload_keys(item))
    elif isinstance(value, list | tuple):
        for item in value:
            keys.update(_nested_payload_keys(item))
    return keys


def _assert_assembled_closed(workflow: WorkflowDef) -> None:
    payload = workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)
    leftover = {"module_id", "imports", "exports", "graph_import", "capability_slots", "capability_slot"}
    found = leftover & _nested_payload_keys(payload)
    if found:
        raise WorkflowAssemblyError(f"assembled workflow retains module metadata: {sorted(found)[0]}")
    for graph in workflow.graphs.values():
        for node in graph.nodes.values():
            if node.graph_import is not None:
                raise WorkflowAssemblyError(f"unresolved graph_import: {node.graph_import}")
            if node.capability_slot is not None:
                raise WorkflowAssemblyError(f"unresolved capability_slot: {node.capability_slot}")


def _binding_entry(
    capability_id: str,
    registries: RegistrySet,
) -> CapabilityBindingEntry:
    entry = registries.capabilities.entries.get(capability_id)
    if entry is None:
        raise WorkflowAssemblyError(f"unregistered capability: {capability_id}")
    if not isinstance(entry, CapabilityBindingEntry):
        raise WorkflowAssemblyError(f"selected capability is not a CapabilityBindingEntry: {capability_id}")
    return entry


def _is_product_owned_capability(owner_id: str, product_id: str) -> bool:
    return owner_id == product_id or owner_id == f"{product_id}.agent"


def _verify_slot_binding(
    binding: WorkflowSlotBinding,
    *,
    module_contract: str,
    manifest: ProductManifest,
    selected: Mapping[str, PluginDescriptor],
    registries: RegistrySet,
) -> None:
    if binding.contract_id != module_contract:
        raise WorkflowAssemblyError(f"slot contract mismatch: {binding.module_id}/{binding.slot}")
    entry = _binding_entry(binding.capability_id, registries)
    if entry.contract_id is None:
        raise WorkflowAssemblyError(f"binding contract missing: {binding.capability_id}")
    if entry.contract_id != binding.contract_id or entry.contract_id != module_contract:
        raise WorkflowAssemblyError(f"slot contract mismatch: {binding.capability_id}")
    if not _is_product_owned_capability(entry.owner_id, manifest.product_id):
        raise WorkflowAssemblyError(f"capability owner is not product-owned: {binding.capability_id}")
    if entry.target_provenance.owner_id not in selected:
        raise WorkflowAssemblyError(
            f"target handler outside descriptor closure: {entry.target_capability_id}"
        )


def _lower_capability_slots(
    workflow: WorkflowDef,
    *,
    loaded: LoadedModules,
    manifest: ProductManifest,
    descriptors: Mapping[str, PluginDescriptor],
    registries: RegistrySet,
) -> WorkflowDef:
    if manifest.workflow_module is None:
        raise WorkflowAssemblyError("workflow module resources require the modular product form")
    selected = _selected_descriptors(descriptors)
    modules = _modules_by_id(loaded, manifest.workflow_module)
    selected_slots = _selected_slot_contracts(modules)
    bindings = manifest.workflow_slot_bindings
    binding_keys = [(item.module_id, item.slot) for item in bindings]
    if len(binding_keys) != len(set(binding_keys)):
        raise WorkflowAssemblyError("duplicate slot binding")
    binding_set = set(binding_keys)
    selected_set = set(selected_slots)
    for item in bindings:
        if item.module_id not in modules:
            raise WorkflowAssemblyError(f"unknown slot: {item.module_id}/{item.slot}")
    extra = binding_set - selected_set
    if extra:
        module_id, slot = sorted(extra)[0]
        raise WorkflowAssemblyError(f"extra slot binding: {module_id}/{slot}")
    missing = selected_set - binding_set
    if missing:
        module_id, slot = sorted(missing)[0]
        raise WorkflowAssemblyError(f"missing slot binding: {module_id}/{slot}")
    by_key = {(item.module_id, item.slot): item for item in bindings}
    for key, binding in by_key.items():
        _verify_slot_binding(
            binding,
            module_contract=selected_slots[key],
            manifest=manifest,
            selected=selected,
            registries=registries,
        )
    rewritten_graphs: dict[str, GraphDef] = {}
    for graph_id, graph in workflow.graphs.items():
        module_id = _module_id_for_graph(graph_id, modules)
        nodes: dict[str, NodeDef] = {}
        for node_id, node in graph.nodes.items():
            if node.capability_slot is None:
                nodes[node_id] = node
                continue
            key = (module_id, node.capability_slot)
            binding = by_key.get(key)
            if binding is None:
                raise WorkflowAssemblyError(f"unknown slot: {module_id}/{node.capability_slot}")
            payload = node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            payload.pop("capability_slot", None)
            payload["capability"] = binding.capability_id
            nodes[node_id] = NodeDef.model_validate(payload)
        rewritten_graphs[graph_id] = GraphDef(
            max_activations=graph.max_activations,
            start=graph.start,
            nodes=nodes,
            edges=graph.edges,
        )
    return WorkflowDef(
        name=workflow.name,
        entrypoints=workflow.entrypoints,
        schemas=workflow.schemas,
        resources=workflow.resources,
        effects=workflow.effects,
        retry=workflow.retry,
        timeout=workflow.timeout,
        graphs=_sorted_mapping(rewritten_graphs),
    )


def assemble_product_workflow(
    *,
    manifest: ProductManifest,
    descriptors: Mapping[str, PluginDescriptor],
    registries: RegistrySet,
) -> WorkflowDef:
    loaded = _load_authenticated_modules(
        manifest=manifest,
        descriptors=descriptors,
        registries=registries,
    )
    lowered = _lower_module_symbols(loaded, manifest=manifest, registries=registries)
    workflow = _lower_capability_slots(
        lowered,
        loaded=loaded,
        manifest=manifest,
        descriptors=descriptors,
        registries=registries,
    )
    _assert_assembled_closed(workflow)
    return workflow
