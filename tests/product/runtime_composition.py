from __future__ import annotations

import builtins
from collections.abc import Mapping
from importlib import metadata
import importlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import PluginDescriptor, ProviderSource, TaskHandler

_CALLBACK_REGISTRY_NAME = "_assurance_product_runtime_test_callbacks"
_CALLBACKS: dict[str, Mapping[str, TaskHandler]] = {}
setattr(builtins, _CALLBACK_REGISTRY_NAME, _CALLBACKS)

_PLUGIN_OWNERS = (
    "assurance.product.agent",
    "assurance.intake",
    "assurance.generation",
    "assurance.execution",
    "assurance.quality",
    "assurance.healing",
    "assurance.improvement",
)


class _MetadataProvider:
    def __init__(self, distribution_name: str, distribution: metadata.Distribution) -> None:
        self._distribution_name = distribution_name
        self._distribution = distribution

    def distribution(self, name: str) -> metadata.Distribution:
        if name != self._distribution_name:
            raise metadata.PackageNotFoundError(name)
        return self._distribution


def _owner_for(capability_id: str) -> str:
    for owner in _PLUGIN_OWNERS:
        if capability_id.startswith(f"{owner}."):
            return owner
    raise AssertionError(f"workflow capability is not owned by a test plugin: {capability_id}")


def _handlers_by_owner(handlers: Mapping[str, TaskHandler]) -> dict[str, dict[str, TaskHandler]]:
    grouped: dict[str, dict[str, TaskHandler]] = {}
    for capability_id, handler in handlers.items():
        grouped.setdefault(_owner_for(capability_id), {})[capability_id] = handler
    return grouped


_PERMISSIVE_SCHEMA = b"true"


def _workflow_schema_ids(workflow: WorkflowDef) -> tuple[str, ...]:
    ids: set[str] = set(workflow.schemas)
    for graph in workflow.graphs.values():
        for node in graph.nodes.values():
            if node.input_schema is not None:
                ids.add(node.input_schema)
            if node.output_schema is not None:
                ids.add(node.output_schema)
    return tuple(sorted(ids))


def _owner_for_schema(schema_id: str) -> str:
    for owner in sorted(_PLUGIN_OWNERS, key=len, reverse=True):
        if schema_id.startswith(f"{owner}."):
            return owner
    raise AssertionError(f"workflow schema is not owned by a test plugin: {schema_id}")


def _schemas_by_owner(workflow: WorkflowDef) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for schema_id in _workflow_schema_ids(workflow):
        grouped.setdefault(_owner_for_schema(schema_id), []).append(schema_id)
    return {owner: tuple(sorted(ids)) for owner, ids in grouped.items()}


def resolve_workflow_composition(
    workflow: dict[str, object],
    handlers: Mapping[str, TaskHandler],
) -> FrozenComposition:
    parsed_workflow = WorkflowDef.model_validate(workflow)
    identity = uuid.uuid4().hex
    distribution_name = f"assurance-product-runtime-test-{identity}"
    package_name = f"assurance_product_runtime_test_{identity}"
    product_entrypoint = f"product-{identity}"
    source_root = Path(tempfile.mkdtemp(prefix="assurance-product-runtime-source-")).resolve()
    package_root = source_root / package_name
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    provider_value = f"{package_name}.provider"
    product_declaration_path = f"{package_name}/product-declaration.json"
    grouped = _handlers_by_owner(handlers)
    schemas_by_owner = _schemas_by_owner(parsed_workflow)
    owners = tuple(owner for owner in _PLUGIN_OWNERS if owner in grouped or owner in schemas_by_owner)
    product_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name=product_entrypoint,
        entrypoint_value=f"{provider_value}:RuntimeProduct",
        declaration_path=product_declaration_path,
        import_roots=("",),
    )
    plugin_specs: list[tuple[str, str, str, PluginDescriptor, ProviderSource, dict[str, TaskHandler]]] = []
    for index, owner in enumerate(owners):
        plugin_entrypoint = f"plugin-{identity}-{index}"
        declaration_path = f"{package_name}/plugin-declaration-{index}.json"
        class_name = f"RuntimePlugin{index}"
        plugin_source = ProviderSource(
            distribution=distribution_name,
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_entrypoint,
            entrypoint_value=f"{provider_value}:{class_name}",
            declaration_path=declaration_path,
            import_roots=("",),
        )
        descriptor = PluginDescriptor(
            schema_version="1",
            source=plugin_source,
            plugin_id=owner,
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=tuple(sorted(grouped.get(owner, {}))),
            commit_validators=(),
            schemas=schemas_by_owner.get(owner, ()),
        )
        plugin_specs.append(
            (
                plugin_entrypoint,
                declaration_path,
                class_name,
                descriptor,
                plugin_source,
                grouped.get(owner, {}),
            )
        )
    manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id="test.product",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=tuple(PluginRequirement(plugin_id=owner, version_specifier="==1.0.0") for owner in owners),
        entrypoints=dict(parsed_workflow.entrypoints),
        configuration={},
        workflow=parsed_workflow,
    )
    manifest_document = manifest.model_dump(mode="json")
    manifest_document["workflow"] = parsed_workflow.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )
    (source_root / product_declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "kind": "product",
                "manifest": manifest_document,
                "schema_version": "1",
                "source": product_source.model_dump(mode="json"),
            }
        )
    )
    callback_key = f"product-{identity}"
    _CALLBACKS[callback_key] = dict(handlers)
    provider_lines = [
        "import builtins",
        "import json",
        "from graph_engine.composition import ProductManifest",
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor, SchemaContribution",
        f"_all_callbacks = getattr(builtins, {_CALLBACK_REGISTRY_NAME!r})[{callback_key!r}]",
        "class _DelegatingHandler:",
        "    def __init__(self, delegate):",
        "        self._delegate = delegate",
        "    async def execute(self, request, context):",
        "        return await self._delegate.execute(request, context)",
        "class RuntimeProduct:",
        "    @staticmethod",
        "    def manifest():",
        f"        return ProductManifest.model_validate(json.loads({json.dumps(manifest_document, sort_keys=True)!r}))",
    ]
    plugin_entry_lines = []
    for (
        plugin_entrypoint,
        declaration_path,
        class_name,
        descriptor,
        plugin_source,
        owner_handlers,
    ) in plugin_specs:
        (source_root / declaration_path).write_bytes(
            canonical_json_bytes(
                {
                    "descriptor": descriptor.model_dump(mode="json"),
                    "kind": "plugin",
                    "schema_version": "1",
                    "source": plugin_source.model_dump(mode="json"),
                }
            )
        )
        handler_keys = json.dumps(sorted(owner_handlers), sort_keys=True)
        descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
        owner_schema_ids = json.dumps(list(descriptor.schemas), sort_keys=True)
        provider_lines.extend(
            [
                f"class {class_name}:",
                "    @staticmethod",
                "    def descriptor():",
                f"        return PluginDescriptor.model_validate(json.loads({descriptor_json!r}))",
                "    @staticmethod",
                "    def contribute(_ports):",
                f"        keys = {handler_keys}",
                f"        schema_ids = {owner_schema_ids}",
                "        return PluginContribution(",
                "            task_handlers={key: _DelegatingHandler(_all_callbacks[key]) for key in keys},",
                "            schemas=tuple(",
                "                SchemaContribution(schema_id, 'application/schema+json', b'true')",
                "                for schema_id in schema_ids",
                "            ),",
                "        )",
            ]
        )
        plugin_entry_lines.append(f"{plugin_entrypoint} = {provider_value}:{class_name}")
    (package_root / "provider.py").write_text("\n".join(provider_lines) + "\n", encoding="utf-8")
    source_files = tuple(
        sorted(path.relative_to(source_root).as_posix() for path in source_root.rglob("*") if path.is_file())
    )
    metadata_root = Path(tempfile.mkdtemp(prefix="assurance-product-runtime-metadata-"))
    dist_info = metadata_root / f"{package_name}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[graph_engine.products]\n"
        f"{product_entrypoint} = {provider_value}:RuntimeProduct\n"
        "[graph_engine.plugins]\n" + "".join(f"{line}\n" for line in plugin_entry_lines),
        encoding="utf-8",
    )
    sys.path.append(str(source_root))
    importlib.invalidate_caches()
    return RegistryPlatform(
        metadata_provider=_MetadataProvider(
            distribution_name,
            metadata.Distribution.at(dist_info),
        )
    ).resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                distribution=distribution_name,
                entrypoint_name=product_entrypoint,
                declaration_path=product_declaration_path,
                source_root=source_root,
                source_files=source_files,
            ),
            plugins=tuple(
                EditableWheelPluginSource(
                    distribution=distribution_name,
                    entrypoint_name=plugin_entrypoint,
                    declaration_path=declaration_path,
                    source_root=source_root,
                    source_files=source_files,
                )
                for plugin_entrypoint, declaration_path, _class_name, _descriptor, _source, _handlers in plugin_specs
            ),
        )
    )
