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
from graph_engine.plugin_api import PluginDescriptor, ProviderSource, TaskHandler

_CALLBACK_REGISTRY_NAME = "_assurance_product_runtime_test_callbacks"
_VALIDATOR_REGISTRY_NAME = "_assurance_product_runtime_test_validators"
_CALLBACKS: dict[str, Mapping[str, TaskHandler]] = {}
_VALIDATORS: dict[str, Mapping[str, object]] = {}
setattr(builtins, _CALLBACK_REGISTRY_NAME, _CALLBACKS)
setattr(builtins, _VALIDATOR_REGISTRY_NAME, _VALIDATORS)

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
    raise AssertionError(f"capability is not owned by a test plugin: {capability_id}")


def _handlers_by_owner(handlers: Mapping[str, TaskHandler]) -> dict[str, dict[str, TaskHandler]]:
    grouped: dict[str, dict[str, TaskHandler]] = {}
    for capability_id, handler in handlers.items():
        grouped.setdefault(_owner_for(capability_id), {})[capability_id] = handler
    return grouped


def resolve_workflow_composition(
    workflow: dict[str, object],
    handlers: Mapping[str, TaskHandler],
    *,
    commit_validators: Mapping[str, object] | None = None,
) -> FrozenComposition:
    del workflow
    identity = uuid.uuid4().hex
    distribution_name = f"assurance-product-runtime-test-{identity}"
    package_name = f"assurance_product_runtime_test_{identity}"
    product_entrypoint = f"product-{identity}"
    factory_symbol = f"{package_name}.provider:build_runtime_graphs"
    source_root = Path(tempfile.mkdtemp(prefix="assurance-product-runtime-source-")).resolve()
    package_root = source_root / package_name
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    provider_value = f"{package_name}.provider"
    product_declaration_path = f"{package_name}/product-declaration.json"
    grouped = _handlers_by_owner(handlers)
    validators = dict(commit_validators or {})
    validator_owners = {
        owner: {key: value for key, value in validators.items() if key.startswith(f"{owner}.")}
        for owner in _PLUGIN_OWNERS
    }
    owners = tuple(owner for owner in _PLUGIN_OWNERS if owner in grouped or validator_owners.get(owner))
    product_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name=product_entrypoint,
        entrypoint_value=f"{provider_value}:RuntimeProduct",
        declaration_path=product_declaration_path,
        import_roots=("", package_name),
    )
    plugin_specs: list[
        tuple[str, str, str, PluginDescriptor, ProviderSource, dict[str, TaskHandler], dict[str, object]]
    ] = []
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
            import_roots=("", package_name),
        )
        descriptor = PluginDescriptor(
            schema_version="1",
            source=plugin_source,
            plugin_id=owner,
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=tuple(sorted(grouped.get(owner, {}))),
            commit_validators=tuple(sorted(validator_owners.get(owner, {}))),
            schemas=(),
        )
        plugin_specs.append(
            (
                plugin_entrypoint,
                declaration_path,
                class_name,
                descriptor,
                plugin_source,
                grouped.get(owner, {}),
                validator_owners.get(owner, {}),
            )
        )
    manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id="test.product",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=tuple(PluginRequirement(plugin_id=owner, version_specifier="==1.0.0") for owner in owners)
        or (PluginRequirement(plugin_id="assurance.intake", version_specifier="==1.0.0"),),
        entrypoints={"main": "root"},
        configuration={},
        graph_factory_symbol=factory_symbol,
    )
    manifest_document = manifest.model_dump(mode="json")
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
    _VALIDATORS[callback_key] = dict(validators)
    provider_lines = [
        "from dataclasses import dataclass",
        "from typing import TypedDict",
        "from langgraph.graph import END, START, StateGraph",
        "from graph_engine.boot.generic import entrypoint_digest",
        "from graph_engine.boot.graph_revision import EntrypointGraphContract",
        "from graph_engine.composition import ProductManifest",
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor",
        "import builtins",
        "import json",
        f"_all_callbacks = getattr(builtins, {_CALLBACK_REGISTRY_NAME!r})[{callback_key!r}]",
        f"_all_validators = getattr(builtins, {_VALIDATOR_REGISTRY_NAME!r})[{callback_key!r}]",
        "class RuntimeState(TypedDict, total=False):",
        "    ok: bool",
        "@dataclass(frozen=True, slots=True)",
        "class RuntimeGraphs:",
        "    entrypoints: dict",
        "    contracts: dict",
        "def build_runtime_graphs(context, features=None):",
        "    del features",
        "    builder = StateGraph(RuntimeState)",
        "    builder.add_node('run', lambda state: {'ok': True})",
        "    builder.add_edge(START, 'run')",
        "    builder.add_edge('run', END)",
        "    contracts = {'main': EntrypointGraphContract(",
        "        name='main',",
        f"        input_model='{package_name}.provider.RuntimeState',",
        f"        output_model='{package_name}.provider.RuntimeState',",
        f"        state_model='{package_name}.provider.RuntimeState',",
        "        input_schema_digest=entrypoint_digest('main', 'input'),",
        "        output_schema_digest=entrypoint_digest('main', 'output'),",
        "        state_schema_digest=entrypoint_digest('main', 'state'),",
        "        state_schema_version='1',",
        "        recursion_limit=32,",
        "    )}",
        "    return RuntimeGraphs({'main': context.compile_root(builder)}, contracts)",
        "class _DelegatingHandler:",
        "    def __init__(self, delegate):",
        "        self._delegate = delegate",
        "    async def execute(self, request, context):",
        "        return await self._delegate.execute(request, context)",
        "class _DelegatingValidator:",
        "    def __init__(self, delegate):",
        "        self._delegate = delegate",
        "    def validate(self, staged, context):",
        "        return self._delegate.validate(staged, context)",
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
        owner_validators,
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
        validator_keys = json.dumps(sorted(owner_validators), sort_keys=True)
        descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
        provider_lines.extend(
            [
                f"class {class_name}:",
                "    @staticmethod",
                "    def descriptor():",
                f"        return PluginDescriptor.model_validate(json.loads({descriptor_json!r}))",
                "    @staticmethod",
                "    def contribute(_ports):",
                f"        keys = {handler_keys}",
                f"        validator_ids = {validator_keys}",
                "        return PluginContribution(",
                "            task_handlers={key: _DelegatingHandler(_all_callbacks[key]) for key in keys},",
                "            commit_validators={key: _DelegatingValidator(_all_validators[key]) for key in validator_ids},",
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
                for plugin_entrypoint, declaration_path, _class_name, _descriptor, _source, _handlers, _validators in plugin_specs
            ),
        )
    )
