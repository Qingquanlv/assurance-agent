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


class _MetadataProvider:
    def __init__(self, distribution_name: str, distribution: metadata.Distribution) -> None:
        self._distribution_name = distribution_name
        self._distribution = distribution

    def distribution(self, name: str) -> metadata.Distribution:
        if name != self._distribution_name:
            raise metadata.PackageNotFoundError(name)
        return self._distribution


def resolve_workflow_composition(
    workflow: dict[str, object],
    handlers: Mapping[str, TaskHandler],
) -> FrozenComposition:
    parsed_workflow = WorkflowDef.model_validate(workflow)
    identity = uuid.uuid4().hex
    distribution_name = f"assurance-product-runtime-test-{identity}"
    package_name = f"assurance_product_runtime_test_{identity}"
    entrypoint_name = f"product-{identity}"
    source_root = Path(tempfile.mkdtemp(prefix="assurance-product-runtime-source-")).resolve()
    package_root = source_root / package_name
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    provider_value = f"{package_name}.provider"
    product_declaration_path = f"{package_name}/product-declaration.json"
    plugin_declaration_path = f"{package_name}/plugin-declaration.json"
    product_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"{provider_value}:RuntimeProduct",
        declaration_path=product_declaration_path,
        import_roots=("",),
    )
    plugin_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"{provider_value}:RuntimePlugin",
        declaration_path=plugin_declaration_path,
        import_roots=("",),
    )
    manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id="test.product",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=(PluginRequirement(plugin_id="assurance.product.agent", version_specifier="==1.0.0"),),
        entrypoints=dict(parsed_workflow.entrypoints),
        configuration={},
        workflow=parsed_workflow,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=plugin_source,
        plugin_id="assurance.product.agent",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=tuple(sorted(handlers)),
        commit_validators=(),
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
    (source_root / plugin_declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "descriptor": descriptor.model_dump(mode="json"),
                "kind": "plugin",
                "schema_version": "1",
                "source": plugin_source.model_dump(mode="json"),
            }
        )
    )
    callback_key = f"product-{identity}"
    _CALLBACKS[callback_key] = dict(handlers)
    manifest_json = json.dumps(manifest_document, sort_keys=True)
    descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
    (package_root / "provider.py").write_text(
        "import builtins\n"
        "import json\n"
        "from graph_engine.composition import ProductManifest\n"
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
        f"_callbacks = getattr(builtins, {_CALLBACK_REGISTRY_NAME!r})[{callback_key!r}]\n"
        "class _DelegatingHandler:\n"
        "    def __init__(self, delegate):\n"
        "        self._delegate = delegate\n"
        "    async def execute(self, request, context):\n"
        "        return await self._delegate.execute(request, context)\n"
        "class RuntimeProduct:\n"
        "    @staticmethod\n"
        "    def manifest():\n"
        f"        return ProductManifest.model_validate(json.loads({manifest_json!r}))\n"
        "class RuntimePlugin:\n"
        "    @staticmethod\n"
        "    def descriptor():\n"
        f"        return PluginDescriptor.model_validate(json.loads({descriptor_json!r}))\n"
        "    @staticmethod\n"
        "    def contribute(_ports):\n"
        "        return PluginContribution(\n"
        "            task_handlers={key: _DelegatingHandler(value) for key, value in _callbacks.items()},\n"
        "        )\n",
        encoding="utf-8",
    )
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
        f"{entrypoint_name} = {provider_value}:RuntimeProduct\n"
        "[graph_engine.plugins]\n"
        f"{entrypoint_name} = {provider_value}:RuntimePlugin\n",
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
                entrypoint_name=entrypoint_name,
                declaration_path=product_declaration_path,
                source_root=source_root,
                source_files=source_files,
            ),
            plugins=(
                EditableWheelPluginSource(
                    distribution=distribution_name,
                    entrypoint_name=entrypoint_name,
                    declaration_path=plugin_declaration_path,
                    source_root=source_root,
                    source_files=source_files,
                ),
            ),
        )
    )
