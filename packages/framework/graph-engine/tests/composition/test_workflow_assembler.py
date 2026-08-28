from __future__ import annotations

import hashlib
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.models import (
    AuthenticatedContribution,
    CapabilityRegistry,
    ContributionAuthority,
    EffectRegistry,
    ExecutableAuthority,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    PluginRequirement,
    ProductManifest,
    RegistrySet,
    ResourceEntry,
    ResourceRegistry,
    SchemaEntry,
    SchemaRegistry,
    SourceEntry,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRegistry,
    SourceRole,
    SourceSnapshot,
    WorkflowModuleRequirement,
    WorkflowSlotBinding,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries as _build_authenticated_registries
from graph_engine.composition.workflow_assembler import (
    LoadedModule,
    LoadedModules,
    WorkflowAssemblyError,
    _load_authenticated_modules,
    _lower_module_symbols,
    assemble_product_workflow,
)
from graph_engine.graph import parse_workflow_module
from graph_engine.graph.compiler import CompileError, compile_workflow
from graph_engine.graph.module_schema import WorkflowModuleDef
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    PluginContribution,
    PluginDescriptor,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"

PRODUCT_MODULE = """
schema_version: "1"
role: product
name: toy
owner_id: toy.product
module_id: toy.product.workflow
module_version: 1.0.0
entrypoints: {main: root}
imports:
  run:
    owner_id: toy.feature
    module_id: toy.feature.workflow
    export: run
exports: {}
capability_slots: {}
schemas:
  - toy.feature.workflow.run.input.v1
  - toy.feature.workflow.run.output.v1
resources: []
effects: []
retry:
  once: {max_attempts: 1}
timeout:
  short: {run_seconds: 30}
graphs:
  root:
    max_activations: 2
    start: child
    nodes:
      child:
        kind: subgraph
        graph_import: run
        input_schema: toy.feature.workflow.run.input.v1
        output_schema: toy.feature.workflow.run.output.v1
        output_projection:
          type: child_output_pointer
          pointer: ""
      done: {kind: end}
    edges:
      - {from: child, to: done}
"""


def _feature_yaml(
    owner_id: str,
    module_id: str,
    *,
    version: str = "1.0.0",
    retry_max_attempts: int = 1,
    timeout_run_seconds: float = 30,
    slotted: bool = False,
    graph: str = "run",
) -> bytes:
    work_nodes = """      done: {kind: end}"""
    work_edges = "    edges: []"
    slots = "capability_slots: {}"
    start = "done"
    if slotted:
        slots = f"""capability_slots:
  worker.execute:
    contract_id: {owner_id}.agent.worker.v1"""
        start = "work"
        work_nodes = """      work:
        kind: task
        capability_slot: worker.execute
        retry: once
        timeout: short
      done: {kind: end}"""
        work_edges = """    edges:
      - {from: work, to: done}"""
    return f"""
schema_version: "1"
role: feature
owner_id: {owner_id}
module_id: {module_id}
module_version: {version}
exports:
  run:
    graph: {graph}
    input_schema: {module_id}.run.input.v1
    output_schema: {module_id}.run.output.v1
    output_projection:
      type: child_output_pointer
      pointer: ""
{slots}
schemas:
  - {module_id}.run.input.v1
  - {module_id}.run.output.v1
resources: []
effects: []
retry:
  once: {{max_attempts: {retry_max_attempts}}}
timeout:
  short: {{run_seconds: {timeout_run_seconds}}}
graphs:
  {graph}:
    max_activations: 2
    start: {start}
    nodes:
{work_nodes}
{work_edges}
""".encode()


def _resource(
    resource_id: str,
    owner_id: str,
    content: bytes,
    *,
    media_type: str = MODULE_MIME,
) -> ResourceEntry:
    frozen = bytes(content)
    return ResourceEntry(
        resource_id=resource_id,
        owner_id=owner_id,
        media_type=media_type,
        content=frozen,
        sha256=hashlib.sha256(frozen).hexdigest(),
    )


def _feature_resource(
    owner_id: str,
    module_id: str,
    *,
    resource_id: str | None = None,
    version: str = "1.0.0",
    media_type: str = MODULE_MIME,
    slotted: bool = False,
    retry_max_attempts: int = 1,
    timeout_run_seconds: float = 30,
    graph: str = "run",
) -> ResourceEntry:
    return _resource(
        resource_id or f"{module_id}.module",
        owner_id,
        _feature_yaml(
            owner_id,
            module_id,
            version=version,
            slotted=slotted,
            retry_max_attempts=retry_max_attempts,
            timeout_run_seconds=timeout_run_seconds,
            graph=graph,
        ),
        media_type=media_type,
    )


def _descriptor(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    resources: tuple[str, ...] = (),
) -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version=version,
        engine_api="1.0.0",
        task_handlers=(),
        commit_validators=(),
        resources=resources,
    )


def _requirement(
    module_id: str,
    owner_id: str,
    resource_id: str | None = None,
) -> WorkflowModuleRequirement:
    return WorkflowModuleRequirement(
        module_id=module_id,
        owner_id=owner_id,
        resource_id=resource_id or f"{module_id}.module",
    )


def _slot_binding(
    *,
    module_id: str = "toy.feature.workflow",
    slot: str = "worker.execute",
    capability_id: str = "toy.product.agent.worker.execute",
    contract_id: str = "toy.feature.agent.worker.v1",
) -> WorkflowSlotBinding:
    return WorkflowSlotBinding(
        module_id=module_id,
        slot=slot,
        capability_id=capability_id,
        contract_id=contract_id,
    )


def _manifest(
    *requirements: WorkflowModuleRequirement,
    workflow_module: str | WorkflowModuleDef | None = None,
    workflow_slot_bindings: tuple[WorkflowSlotBinding, ...] = (),
) -> ProductManifest:
    module = (
        workflow_module
        if isinstance(workflow_module, WorkflowModuleDef)
        else parse_workflow_module(workflow_module if workflow_module is not None else PRODUCT_MODULE)
    )
    return ProductManifest.model_validate(
        {
            "schema_version": "1",
            "source": None,
            "product_id": module.owner_id,
            "product_version": module.module_version,
            "engine_api": "2.0",
            "plugins": (PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),),
            "entrypoints": dict(module.entrypoints),
            "workflow_module": module,
            "workflow_module_resources": requirements,
            "workflow_slot_bindings": workflow_slot_bindings,
        }
    )


def _plugin_source(plugin_id: str, *, version: str = "1.0.0") -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.CONFIG_TREE,
            root=Path(f"/sources/{plugin_id}"),
            plugin_id=plugin_id,
            plugin_version=version,
        ),
        (),
    )


def _schema_entry(schema_id: str) -> SchemaEntry:
    return SchemaEntry.from_content(
        schema_id=schema_id,
        owner_id=".".join(schema_id.split(".")[:2]),
        media_type="application/schema+json",
        content=b'{"type":"object","additionalProperties":false}',
    )


def _schema_entries(*modules: WorkflowModuleDef) -> dict[str, SchemaEntry]:
    schema_ids: set[str] = set()
    for module in modules:
        schema_ids.update(module.schemas)
        for export in module.exports.values():
            schema_ids.add(export.input_schema)
            schema_ids.add(export.output_schema)
    return {schema_id: _schema_entry(schema_id) for schema_id in schema_ids}


class _Handler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"ok": True})


def _wheel_source(plugin_id: str) -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=Path(f"/sources/{plugin_id}"),
            distribution=plugin_id.replace(".", "-"),
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=f"{plugin_id.replace('.', '_')}:provider",
            declaration_path=f"{plugin_id.replace('.', '_')}/plugin-declaration.json",
            import_roots=("",),
            plugin_id=plugin_id,
            plugin_version="1.0.0",
        ),
        (),
    )


def _source_key(snapshot: SourceSnapshot) -> SourceKey:
    role = SourceRole.CONFIG if snapshot.identity.kind is SourceKind.CONFIG_TREE else SourceRole.PLUGIN
    owner_id = snapshot.identity.plugin_id
    assert owner_id is not None
    return SourceKey(role, owner_id)


def _proof(snapshot: SourceSnapshot, kind: ExecutableKind, registry_id: str) -> ExecutableProvenance:
    source_key = _source_key(snapshot)
    return ExecutableProvenance.create(
        kind=kind,
        registry_id=registry_id,
        owner_id=source_key.owner_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        module=ExecutableModuleProvenance(
            module_name=f"{source_key.owner_id.replace('.', '_')}.implementation",
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="implementation.py",
            authenticated_locations=(),
            physical_sha256="0" * 64,
            source_digest=snapshot.digest,
        ),
        callable_path="implementation:Handler.execute",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )


def _authenticated(snapshot: SourceSnapshot, contribution: PluginContribution) -> AuthenticatedContribution:
    source_key = _source_key(snapshot)
    proofs = tuple(
        _proof(snapshot, ExecutableKind.TASK_HANDLER, capability_id)
        for capability_id in contribution.task_handlers
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=source_key.owner_id,
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=tuple(contribution.task_handlers),
        commit_validators=(),
        schemas=tuple(item.schema_id for item in contribution.schemas),
        resources=tuple(item.resource_id for item in contribution.resources),
        bindings=tuple(item.capability_id for item in contribution.bindings),
    )
    authorities = tuple(
        ExecutableAuthority(
            executable=contribution.task_handlers[proof.registry_id],
            function=type(contribution.task_handlers[proof.registry_id]).__dict__[proof.kind.slot],
            bound_self=contribution.task_handlers[proof.registry_id],
            descriptor=type(contribution.task_handlers[proof.registry_id]).__dict__[proof.kind.slot],
            provenance=proof,
        )
        for proof in proofs
    )
    return AuthenticatedContribution(
        owner_id=source_key.owner_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=proofs,
        authority=ContributionAuthority(
            provider_binding=object() if source_key.role is SourceRole.PLUGIN else None,
            descriptor=descriptor,
            owner_id=source_key.owner_id,
            source_key=source_key,
            source_digest=snapshot.digest,
            contribution=contribution,
            authorities=authorities,
        ),
    )


def _binding_registry(
    *,
    capability_id: str = "toy.product.agent.worker.execute",
    target_capability_id: str = "toy.runtime.execute",
    contract_id: str | None = "toy.feature.agent.worker.v1",
    binding_owner: str = "toy.product",
    target_owner: str = "toy.runtime",
    as_direct_handler: bool = False,
) -> RegistrySet:
    runtime = _wheel_source(target_owner)
    handler = _Handler()
    contributions = [
        _authenticated(
            runtime,
            PluginContribution(task_handlers={target_capability_id: handler}),
        )
    ]
    sources = [runtime]
    order = [target_owner]
    if not as_direct_handler:
        product = _plugin_source(binding_owner)
        contributions.append(
            _authenticated(
                product,
                PluginContribution(
                    bindings=(
                        CapabilityBindingContribution(
                            capability_id=capability_id,
                            target_capability_id=target_capability_id,
                            contract_id=contract_id,
                        ),
                    )
                ),
            )
        )
        sources.append(product)
        order.append(binding_owner)
    return _build_authenticated_registries(tuple(sources), tuple(contributions), tuple(order))


def _registries(
    *resources: ResourceEntry,
    schemas: dict[str, SchemaEntry] | None = None,
    binding_registry: RegistrySet | None = None,
) -> RegistrySet:
    sources: dict[SourceKey, SourceEntry] = {}
    for resource in resources:
        key = SourceKey(SourceRole.CONFIG, resource.owner_id)
        if key in sources:
            continue
        snapshot = _plugin_source(resource.owner_id)
        sources[key] = SourceEntry(source_key=key, snapshot=snapshot)
    capabilities = CapabilityRegistry.empty()
    if binding_registry is not None:
        sources.update(binding_registry.sources.entries)
        capabilities = binding_registry.capabilities
    return RegistrySet(
        sources=SourceRegistry(sources),
        capabilities=capabilities,
        schemas=SchemaRegistry(entries=schemas or {}),
        resources=ResourceRegistry({item.resource_id: item for item in resources}),
        effects=EffectRegistry(entries={}),
    )


def _selected(*resources: ResourceEntry) -> dict[str, PluginDescriptor]:
    return {
        resource.owner_id: _descriptor(resource.owner_id, resources=(resource.resource_id,))
        for resource in resources
    }


def test_missing_required_resource_fails() -> None:
    requirement = _requirement("toy.feature.workflow", "toy.feature")
    with pytest.raises(WorkflowAssemblyError, match="missing required resource"):
        _load_authenticated_modules(
            manifest=_manifest(requirement),
            descriptors={"toy.feature": _descriptor("toy.feature")},
            registries=_registries(),
        )


def test_resource_wrong_mime_fails() -> None:
    resource = _feature_resource("toy.feature", "toy.feature.workflow", media_type="text/plain")
    with pytest.raises(WorkflowAssemblyError, match="media type"):
        _load_authenticated_modules(
            manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
            descriptors=_selected(resource),
            registries=_registries(resource),
        )


def test_resource_owner_mismatch_fails() -> None:
    resource = _feature_resource("toy.other", "toy.other.workflow")
    with pytest.raises(WorkflowAssemblyError, match="owner"):
        _load_authenticated_modules(
            manifest=_manifest(
                _requirement(
                    "toy.feature.workflow",
                    "toy.feature",
                    resource.resource_id,
                )
            ),
            descriptors={"toy.feature": _descriptor("toy.feature", resources=(resource.resource_id,))},
            registries=_registries(resource),
        )


def test_resource_module_id_mismatch_fails() -> None:
    resource = _feature_resource("toy.feature", "toy.feature.other")
    with pytest.raises(WorkflowAssemblyError, match="module"):
        _load_authenticated_modules(
            manifest=_manifest(
                _requirement(
                    "toy.feature.workflow",
                    "toy.feature",
                    resource.resource_id,
                )
            ),
            descriptors=_selected(resource),
            registries=_registries(resource),
        )


def test_resource_version_versus_descriptor_mismatch_fails() -> None:
    resource = _feature_resource("toy.feature", "toy.feature.workflow", version="1.0.0")
    with pytest.raises(WorkflowAssemblyError, match="version"):
        _load_authenticated_modules(
            manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
            descriptors={
                "toy.feature": _descriptor(
                    "toy.feature",
                    version="2.0.0",
                    resources=(resource.resource_id,),
                )
            },
            registries=_registries(resource),
        )


def test_duplicate_module_resource_identity_fails() -> None:
    declared = _feature_resource("toy.feature", "toy.feature.workflow")
    duplicate = _resource(
        "toy.feature.workflow.alt",
        "toy.feature",
        _feature_yaml("toy.feature", "toy.feature.workflow"),
    )
    with pytest.raises(WorkflowAssemblyError, match="duplicate"):
        _load_authenticated_modules(
            manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
            descriptors={
                "toy.feature": _descriptor(
                    "toy.feature",
                    resources=(declared.resource_id, duplicate.resource_id),
                )
            },
            registries=_registries(declared, duplicate),
        )


def test_undeclared_extra_module_resource_owned_by_selected_descriptor_fails() -> None:
    declared = _feature_resource("toy.feature", "toy.feature.workflow")
    extra = _feature_resource("toy.feature", "toy.feature.extra")
    with pytest.raises(WorkflowAssemblyError, match="extra"):
        _load_authenticated_modules(
            manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
            descriptors={
                "toy.feature": _descriptor(
                    "toy.feature",
                    resources=(declared.resource_id, extra.resource_id),
                )
            },
            registries=_registries(declared, extra),
        )


def test_ambient_module_resource_owned_by_unselected_descriptor_is_ignored() -> None:
    required = _feature_resource("toy.feature", "toy.feature.workflow")
    ambient = _feature_resource("toy.ambient", "toy.ambient.workflow")
    descriptors = _selected(required)
    left = _load_authenticated_modules(
        manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
        descriptors=descriptors,
        registries=_registries(required),
    )
    right = _load_authenticated_modules(
        manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
        descriptors=descriptors,
        registries=_registries(required, ambient),
    )
    assert canonical_json_bytes(left.canonical_projection()) == canonical_json_bytes(
        right.canonical_projection()
    )
    assert [item.module_id for item in left.modules] == ["toy.feature.workflow"]


def test_frozen_resource_bytes_ignore_later_filesystem_replacement(tmp_path: Path) -> None:
    original = _feature_yaml("toy.feature", "toy.feature.workflow")
    path = tmp_path / "module.yaml"
    path.write_bytes(original)
    resource = _resource("toy.feature.workflow.module", "toy.feature", path.read_bytes())
    path.write_bytes(_feature_yaml("toy.feature", "toy.feature.replaced"))
    loaded = _load_authenticated_modules(
        manifest=_manifest(_requirement("toy.feature.workflow", "toy.feature")),
        descriptors=_selected(resource),
        registries=_registries(resource),
    )
    assert loaded.modules[0].content == original
    assert loaded.modules[0].sha256 == hashlib.sha256(original).hexdigest()
    assert loaded.modules[0].module.module_id == "toy.feature.workflow"
    assert loaded.modules[0].module.module_id != "toy.feature.replaced"


def test_requirement_and_registry_construction_order_is_independent() -> None:
    alpha = _feature_resource("toy.alpha", "toy.alpha.workflow")
    zeta = _feature_resource("toy.zeta", "toy.zeta.workflow")
    specs = {
        "alpha": (alpha, _requirement("toy.alpha.workflow", "toy.alpha")),
        "zeta": (zeta, _requirement("toy.zeta.workflow", "toy.zeta")),
    }
    descriptors = {**_selected(alpha), **_selected(zeta)}
    manifest_ab = _manifest(specs["zeta"][1], specs["alpha"][1])
    manifest_ba = _manifest(specs["alpha"][1], specs["zeta"][1])
    registry_ab = _registries(zeta, alpha)
    registry_ba = _registries(alpha, zeta)
    left = _load_authenticated_modules(manifest=manifest_ab, descriptors=descriptors, registries=registry_ab)
    right = _load_authenticated_modules(manifest=manifest_ba, descriptors=descriptors, registries=registry_ba)
    assert canonical_json_bytes(left.canonical_projection()) == (
        canonical_json_bytes(right.canonical_projection())
    )
    expected = canonical_json_bytes(left.canonical_projection())
    for requirement_order in permutations(("alpha", "zeta")):
        for registry_order in permutations(("alpha", "zeta")):
            loaded = _load_authenticated_modules(
                manifest=_manifest(*(specs[name][1] for name in requirement_order)),
                descriptors=descriptors,
                registries=_registries(*(specs[name][0] for name in registry_order)),
            )
            assert canonical_json_bytes(loaded.canonical_projection()) == expected
    assert [item.module_id for item in left.modules] == [
        "toy.alpha.workflow",
        "toy.zeta.workflow",
    ]


def _payload(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def _dump(raw: dict[str, Any]) -> str:
    return yaml.safe_dump(raw, sort_keys=False)


def _nested_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(_nested_keys(item))
    elif isinstance(value, list | tuple):
        for item in value:
            keys.update(_nested_keys(item))
    return keys


def _capability_slots(workflow: object) -> set[str]:
    slots: set[str] = set()
    graphs = getattr(workflow, "graphs")
    for graph in graphs.values():
        for node in graph.nodes.values():
            slot = getattr(node, "capability_slot", None)
            if slot:
                slots.add(slot)
    return slots


def _lower(
    *resources: ResourceEntry,
    product: str | WorkflowModuleDef | None = None,
) -> WorkflowDef:
    requirements = tuple(
        _requirement(
            parse_workflow_module(resource.content).module_id,
            resource.owner_id,
            resource.resource_id,
        )
        for resource in resources
    )
    manifest = _manifest(*requirements, workflow_module=product)
    module = manifest.workflow_module
    assert module is not None
    parsed_features = [parse_workflow_module(resource.content) for resource in resources]
    registries = _registries(*resources, schemas=_schema_entries(module, *parsed_features))
    loaded = _load_authenticated_modules(
        manifest=manifest,
        descriptors=_selected(*resources),
        registries=registries,
    )
    return _lower_module_symbols(loaded, manifest=manifest, registries=registries)


def _feature_payload(owner_id: str, module_id: str, **kwargs: Any) -> dict[str, Any]:
    return _payload(_feature_yaml(owner_id, module_id, **kwargs).decode())


def test_undeclared_import_alias_fails() -> None:
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    manifest = _manifest(_requirement("toy.feature.workflow", "toy.feature"))
    product = manifest.workflow_module
    assert product is not None
    registries = _registries(
        feature,
        schemas=_schema_entries(product, parse_workflow_module(feature.content)),
    )
    loaded = _load_authenticated_modules(
        manifest=manifest,
        descriptors=_selected(feature),
        registries=registries,
    )
    root = product.graphs["root"]
    child = root.nodes["child"]
    tampered_child = type(child).model_construct(
        **{key: getattr(child, key) for key in type(child).model_fields if key != "graph_import"},
        graph_import="ghost",
    )
    tampered_root = type(root).model_construct(
        max_activations=root.max_activations,
        start=root.start,
        nodes={**root.nodes, "child": tampered_child},
        edges=root.edges,
    )
    tampered_product = WorkflowModuleDef.model_construct(
        schema_version=product.schema_version,
        role=product.role,
        owner_id=product.owner_id,
        module_id=product.module_id,
        module_version=product.module_version,
        name=product.name,
        entrypoints=product.entrypoints,
        imports=product.imports,
        exports=product.exports,
        capability_slots=product.capability_slots,
        schemas=product.schemas,
        resources=product.resources,
        effects=product.effects,
        retry=product.retry,
        timeout=product.timeout,
        graphs={**product.graphs, "root": tampered_root},
    )
    tampered_manifest = ProductManifest.model_construct(
        schema_version=manifest.schema_version,
        source=manifest.source,
        product_id=manifest.product_id,
        product_version=manifest.product_version,
        engine_api=manifest.engine_api,
        plugins=manifest.plugins,
        entrypoints=manifest.entrypoints,
        configuration=manifest.configuration,
        config_plugin_paths=manifest.config_plugin_paths,
        workflow=manifest.workflow,
        workflow_resource_id=manifest.workflow_resource_id,
        workflow_module=tampered_product,
        workflow_module_resources=manifest.workflow_module_resources,
        workflow_slot_bindings=manifest.workflow_slot_bindings,
    )
    with pytest.raises(WorkflowAssemblyError, match="undeclared import alias"):
        _lower_module_symbols(loaded, manifest=tampered_manifest, registries=registries)


def test_missing_import_module_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["imports"] = {
        "ghost": {
            "owner_id": "toy.ghost",
            "module_id": "toy.ghost.workflow",
            "export": "run",
        }
    }
    child = raw["graphs"]["root"]["nodes"]["child"]
    child["graph_import"] = "ghost"
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="missing import module"):
        _lower(feature, product=_dump(raw))


def test_missing_export_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["imports"]["run"]["export"] = "missing"
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="export"):
        _lower(feature, product=_dump(raw))


def test_private_graph_reference_fails() -> None:
    feature_raw = _feature_payload("toy.feature", "toy.feature.workflow")
    feature_raw["graphs"]["helper"] = {
        "max_activations": 1,
        "start": "done",
        "nodes": {"done": {"kind": "end"}},
        "edges": [],
    }
    product_raw = _payload(PRODUCT_MODULE)
    product_raw["imports"]["run"]["export"] = "helper"
    feature = _resource(
        "toy.feature.workflow.module",
        "toy.feature",
        _dump(feature_raw).encode(),
    )
    with pytest.raises(WorkflowAssemblyError, match="private"):
        _lower(feature, product=_dump(product_raw))


def test_duplicate_local_export_import_name_collision_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["imports"]["root"] = raw["imports"].pop("run")
    raw["graphs"]["root"]["nodes"]["child"]["graph_import"] = "root"
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="collision|duplicate"):
        _lower(feature, product=_dump(raw))


def test_module_import_cycle_fails() -> None:
    alpha_raw = _feature_payload("toy.alpha", "toy.alpha.workflow")
    zeta_raw = _feature_payload("toy.zeta", "toy.zeta.workflow")
    alpha_raw["imports"] = {
        "peer": {
            "owner_id": "toy.zeta",
            "module_id": "toy.zeta.workflow",
            "export": "run",
        }
    }
    alpha_raw["graphs"]["run"] = {
        "max_activations": 2,
        "start": "child",
        "nodes": {
            "child": {"kind": "subgraph", "graph_import": "peer"},
            "done": {"kind": "end"},
        },
        "edges": [{"from": "child", "to": "done"}],
    }
    zeta_raw["imports"] = {
        "peer": {
            "owner_id": "toy.alpha",
            "module_id": "toy.alpha.workflow",
            "export": "run",
        }
    }
    zeta_raw["graphs"]["run"] = {
        "max_activations": 2,
        "start": "child",
        "nodes": {
            "child": {"kind": "subgraph", "graph_import": "peer"},
            "done": {"kind": "end"},
        },
        "edges": [{"from": "child", "to": "done"}],
    }
    product_raw = _payload(PRODUCT_MODULE)
    product_raw["imports"] = {
        "alpha": {
            "owner_id": "toy.alpha",
            "module_id": "toy.alpha.workflow",
            "export": "run",
        }
    }
    product_raw["graphs"]["root"]["nodes"]["child"]["graph_import"] = "alpha"
    product_raw["schemas"] = [
        "toy.alpha.workflow.run.input.v1",
        "toy.alpha.workflow.run.output.v1",
    ]
    alpha = _resource("toy.alpha.workflow.module", "toy.alpha", _dump(alpha_raw).encode())
    zeta = _resource("toy.zeta.workflow.module", "toy.zeta", _dump(zeta_raw).encode())
    with pytest.raises(WorkflowAssemblyError, match="cycle"):
        _lower(alpha, zeta, product=_dump(product_raw))


def test_invalid_local_symbol_fails() -> None:
    feature_raw = _feature_payload("toy.feature", "toy.feature.workflow")
    feature_raw["graphs"]["run"] = {
        "max_activations": 2,
        "start": "child",
        "nodes": {
            "child": {"kind": "subgraph", "graph": "missing"},
            "done": {"kind": "end"},
        },
        "edges": [{"from": "child", "to": "done"}],
    }
    feature = _resource(
        "toy.feature.workflow.module",
        "toy.feature",
        _dump(feature_raw).encode(),
    )
    with pytest.raises(WorkflowAssemblyError, match="local symbol|unknown"):
        _lower(feature)


def test_feature_to_feature_graph_import_fails() -> None:
    alpha_raw = _feature_payload("toy.alpha", "toy.alpha.workflow")
    alpha_raw["imports"] = {
        "peer": {
            "owner_id": "toy.zeta",
            "module_id": "toy.zeta.workflow",
            "export": "run",
        }
    }
    alpha_raw["graphs"]["run"] = {
        "max_activations": 2,
        "start": "child",
        "nodes": {
            "child": {"kind": "subgraph", "graph_import": "peer"},
            "done": {"kind": "end"},
        },
        "edges": [{"from": "child", "to": "done"}],
    }
    product_raw = _payload(PRODUCT_MODULE)
    product_raw["imports"] = {
        "alpha": {
            "owner_id": "toy.alpha",
            "module_id": "toy.alpha.workflow",
            "export": "run",
        }
    }
    product_raw["graphs"]["root"]["nodes"]["child"]["graph_import"] = "alpha"
    product_raw["schemas"] = [
        "toy.alpha.workflow.run.input.v1",
        "toy.alpha.workflow.run.output.v1",
        "toy.zeta.workflow.run.input.v1",
        "toy.zeta.workflow.run.output.v1",
    ]
    alpha = _resource("toy.alpha.workflow.module", "toy.alpha", _dump(alpha_raw).encode())
    zeta = _feature_resource("toy.zeta", "toy.zeta.workflow")
    with pytest.raises(WorkflowAssemblyError, match="feature-to-feature|import tables"):
        _lower(alpha, zeta, product=_dump(product_raw))


def test_product_direct_graph_reference_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    child = raw["graphs"]["root"]["nodes"]["child"]
    child.pop("graph_import")
    child["graph"] = "run"
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="direct graph|structural"):
        _lower(feature, product=_dump(raw))


def test_product_direct_capability_reference_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["graphs"]["root"]["nodes"]["work"] = {
        "kind": "task",
        "capability": "toy.feature.do-work",
        "retry": "once",
        "timeout": "short",
    }
    raw["graphs"]["root"]["edges"].append({"from": "done", "to": "work"})
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="capability|structural|task"):
        _lower(feature, product=_dump(raw))


def test_product_task_node_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["graphs"]["root"]["nodes"]["work"] = {
        "kind": "task",
        "capability_slot": "worker.execute",
        "retry": "once",
        "timeout": "short",
    }
    raw["capability_slots"] = {"worker.execute": {"contract_id": "toy.feature.agent.worker.v1"}}
    raw["graphs"]["root"]["edges"].append({"from": "done", "to": "work"})
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="task|structural"):
        _lower(feature, product=_dump(raw))


def test_feature_entrypoint_rejected_for_product_assembly() -> None:
    feature = _feature_resource("toy.feature", "toy.feature.workflow", slotted=True)
    manifest = _manifest(_requirement("toy.feature.workflow", "toy.feature"))
    product = manifest.workflow_module
    assert product is not None
    registries = _registries(
        feature,
        schemas=_schema_entries(product, parse_workflow_module(feature.content)),
    )
    loaded = _load_authenticated_modules(
        manifest=manifest,
        descriptors=_selected(feature),
        registries=registries,
    )
    module = loaded.modules[0].module
    tampered = WorkflowModuleDef.model_construct(
        schema_version=module.schema_version,
        role=module.role,
        owner_id=module.owner_id,
        module_id=module.module_id,
        module_version=module.module_version,
        name=module.name,
        entrypoints={"main": "run"},
        imports=module.imports,
        exports=module.exports,
        capability_slots=module.capability_slots,
        schemas=module.schemas,
        resources=module.resources,
        effects=module.effects,
        retry=module.retry,
        timeout=module.timeout,
        graphs=module.graphs,
    )
    with pytest.raises(WorkflowAssemblyError, match="entrypoint"):
        _lower_module_symbols(
            LoadedModules(
                modules=(
                    LoadedModule(
                        module_id=tampered.module_id,
                        owner_id=tampered.owner_id,
                        resource_id=loaded.modules[0].resource_id,
                        module_version=tampered.module_version,
                        media_type=loaded.modules[0].media_type,
                        content=loaded.modules[0].content,
                        sha256=loaded.modules[0].sha256,
                        module=tampered,
                    ),
                )
            ),
            manifest=manifest,
            registries=registries,
        )


def test_modular_multi_out_node_without_routing_fails() -> None:
    raw = _payload(PRODUCT_MODULE)
    raw["graphs"]["root"]["nodes"]["other"] = {"kind": "end"}
    raw["graphs"]["root"]["edges"].append({"from": "child", "to": "other"})
    feature = _feature_resource("toy.feature", "toy.feature.workflow")
    with pytest.raises(WorkflowAssemblyError, match="routing"):
        _lower(feature, product=_dump(raw))


def test_valid_product_feature_import_lowers_public_symbols() -> None:
    feature = _feature_resource("toy.feature", "toy.feature.workflow", slotted=True)
    registries_feature = parse_workflow_module(feature.content)
    manifest = _manifest(_requirement("toy.feature.workflow", "toy.feature"))
    module = manifest.workflow_module
    assert module is not None
    registries = _registries(feature, schemas=_schema_entries(module, registries_feature))
    loaded = _load_authenticated_modules(
        manifest=manifest,
        descriptors=_selected(feature),
        registries=registries,
    )
    lowered = _lower_module_symbols(loaded, manifest=manifest, registries=registries)
    assert lowered.name == "toy"
    assert lowered.entrypoints == {"main": "toy.product.workflow.graph.root"}
    assert set(lowered.graphs) == {
        "toy.product.workflow.graph.root",
        "toy.feature.workflow.graph.run",
    }
    assert set(lowered.retry) == {
        "toy.product.workflow.retry.once",
        "toy.feature.workflow.retry.once",
    }
    assert set(lowered.timeout) == {
        "toy.product.workflow.timeout.short",
        "toy.feature.workflow.timeout.short",
    }
    child = lowered.graphs["toy.product.workflow.graph.root"].nodes["child"]
    assert child.kind == "subgraph"
    assert child.graph == "toy.feature.workflow.graph.run"
    assert child.graph_import is None
    assert child.input_schema == "toy.feature.workflow.run.input.v1"
    assert child.output_schema == "toy.feature.workflow.run.output.v1"
    assert child.output_projection is not None
    assert child.output_projection.model_dump()["pointer"] == ""
    work = lowered.graphs["toy.feature.workflow.graph.run"].nodes["work"]
    assert work.capability_slot == "worker.execute"
    assert work.retry == "toy.feature.workflow.retry.once"
    assert work.timeout == "toy.feature.workflow.timeout.short"
    payload = lowered.model_dump(mode="json", by_alias=True, exclude_unset=True)
    keys = _nested_keys(payload)
    assert "module_id" not in keys
    assert "imports" not in keys
    assert "exports" not in keys
    assert "graph_import" not in keys
    assert _capability_slots(lowered) == {"worker.execute"}
    with pytest.raises(CompileError, match="unresolved capability_slot"):
        compile_workflow(lowered, registries)


def test_same_named_policies_do_not_collision_across_features() -> None:
    alpha = _resource(
        "toy.alpha.workflow.module",
        "toy.alpha",
        _feature_yaml(
            "toy.alpha",
            "toy.alpha.workflow",
            retry_max_attempts=1,
            timeout_run_seconds=30,
        ),
    )
    zeta = _resource(
        "toy.zeta.workflow.module",
        "toy.zeta",
        _feature_yaml(
            "toy.zeta",
            "toy.zeta.workflow",
            retry_max_attempts=3,
            timeout_run_seconds=9,
        ),
    )
    product_raw = _payload(PRODUCT_MODULE)
    product_raw["imports"] = {
        "alpha": {
            "owner_id": "toy.alpha",
            "module_id": "toy.alpha.workflow",
            "export": "run",
        },
        "zeta": {
            "owner_id": "toy.zeta",
            "module_id": "toy.zeta.workflow",
            "export": "run",
        },
    }
    product_raw["schemas"] = [
        "toy.alpha.workflow.run.input.v1",
        "toy.alpha.workflow.run.output.v1",
        "toy.zeta.workflow.run.input.v1",
        "toy.zeta.workflow.run.output.v1",
    ]
    product_raw["graphs"]["root"] = {
        "max_activations": 2,
        "start": "alpha_child",
        "nodes": {
            "alpha_child": {"kind": "subgraph", "graph_import": "alpha"},
            "zeta_child": {"kind": "subgraph", "graph_import": "zeta"},
            "done": {"kind": "end"},
        },
        "edges": [
            {"from": "alpha_child", "to": "zeta_child"},
            {"from": "zeta_child", "to": "done"},
        ],
    }
    lowered = _lower(alpha, zeta, product=_dump(product_raw))
    assert set(lowered.graphs) >= {
        "toy.alpha.workflow.graph.run",
        "toy.zeta.workflow.graph.run",
    }
    assert lowered.retry["toy.alpha.workflow.retry.once"].max_attempts == 1
    assert lowered.retry["toy.zeta.workflow.retry.once"].max_attempts == 3
    assert lowered.timeout["toy.alpha.workflow.timeout.short"].run_seconds == 30
    assert lowered.timeout["toy.zeta.workflow.timeout.short"].run_seconds == 9
    assert "once" not in lowered.retry
    assert "short" not in lowered.timeout
    assert lowered.graphs["toy.product.workflow.graph.root"].nodes["alpha_child"].graph == (
        "toy.alpha.workflow.graph.run"
    )
    assert lowered.graphs["toy.product.workflow.graph.root"].nodes["zeta_child"].graph == (
        "toy.zeta.workflow.graph.run"
    )


def test_same_module_dangling_policy_reference_fails() -> None:
    feature_raw = _feature_payload("toy.feature", "toy.feature.workflow", slotted=True)
    feature_raw["graphs"]["run"]["nodes"]["work"]["retry"] = "missing"
    feature = _resource(
        "toy.feature.workflow.module",
        "toy.feature",
        _dump(feature_raw).encode(),
    )
    with pytest.raises(WorkflowAssemblyError, match="policy|dangling"):
        _lower(feature)


def _slotted_feature(*, graph: str = "feature-run") -> ResourceEntry:
    return _feature_resource("toy.feature", "toy.feature.workflow", slotted=True, graph=graph)


def _slot_assembly_inputs(
    *,
    feature: ResourceEntry | None = None,
    bindings: tuple[WorkflowSlotBinding, ...] | None = None,
    binding_registry: RegistrySet | None = None,
) -> tuple[ProductManifest, dict[str, PluginDescriptor], RegistrySet]:
    resource = feature or _slotted_feature()
    parsed = parse_workflow_module(resource.content)
    manifest = _manifest(
        _requirement(parsed.module_id, resource.owner_id, resource.resource_id),
        workflow_slot_bindings=(_slot_binding(),) if bindings is None else bindings,
    )
    module = manifest.workflow_module
    assert module is not None
    descriptors = {
        **_selected(resource),
        "toy.runtime": _descriptor("toy.runtime"),
    }
    registries = _registries(
        resource,
        schemas=_schema_entries(module, parsed),
        binding_registry=binding_registry if binding_registry is not None else _binding_registry(),
    )
    return manifest, descriptors, registries


def test_missing_slot_binding_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(bindings=())
    with pytest.raises(WorkflowAssemblyError, match="missing"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_extra_slot_binding_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(
        bindings=(
            _slot_binding(),
            _slot_binding(slot="ghost.execute", contract_id="toy.feature.agent.ghost.v1"),
        )
    )
    with pytest.raises(WorkflowAssemblyError, match="extra"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_duplicate_slot_binding_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs()
    duplicate = _slot_binding()
    tampered = ProductManifest.model_construct(
        schema_version=manifest.schema_version,
        source=manifest.source,
        product_id=manifest.product_id,
        product_version=manifest.product_version,
        engine_api=manifest.engine_api,
        plugins=manifest.plugins,
        entrypoints=manifest.entrypoints,
        configuration=manifest.configuration,
        config_plugin_paths=manifest.config_plugin_paths,
        workflow=manifest.workflow,
        workflow_resource_id=manifest.workflow_resource_id,
        workflow_module=manifest.workflow_module,
        workflow_module_resources=manifest.workflow_module_resources,
        workflow_slot_bindings=(duplicate, duplicate),
    )
    with pytest.raises(WorkflowAssemblyError, match="duplicate"):
        assemble_product_workflow(manifest=tampered, descriptors=descriptors, registries=registries)


def test_unknown_slot_binding_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(
        bindings=(_slot_binding(module_id="toy.ghost.workflow"),)
    )
    with pytest.raises(WorkflowAssemblyError, match="unknown"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_slot_contract_mismatch_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(
        bindings=(_slot_binding(contract_id="toy.feature.agent.other.v1"),)
    )
    with pytest.raises(WorkflowAssemblyError, match="contract"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_selected_slot_capability_must_be_binding_entry() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(
        bindings=(_slot_binding(capability_id="toy.runtime.execute"),),
        binding_registry=_binding_registry(as_direct_handler=True),
    )
    with pytest.raises(WorkflowAssemblyError, match="CapabilityBindingEntry"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_slot_binding_contract_missing_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs(
        binding_registry=_binding_registry(contract_id=None),
    )
    with pytest.raises(WorkflowAssemblyError, match="contract"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_slot_target_handler_outside_descriptor_closure_fails() -> None:
    manifest, descriptors, registries = _slot_assembly_inputs()
    descriptors.pop("toy.runtime")
    with pytest.raises(WorkflowAssemblyError, match="descriptor|closure|target"):
        assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)


def test_valid_slot_lowers_to_concrete_capability() -> None:
    feature = _slotted_feature()
    manifest, descriptors, registries = _slot_assembly_inputs(feature=feature)
    assembled = assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)
    task = assembled.graphs["toy.feature.workflow.graph.feature-run"].nodes["work"]
    assert task.capability == "toy.product.agent.worker.execute"
    assert task.capability_slot is None
    payload = assembled.model_dump(mode="json", by_alias=True, exclude_unset=True)
    keys = _nested_keys(payload)
    assert "module_id" not in keys
    assert "imports" not in keys
    assert "exports" not in keys
    assert "graph_import" not in keys
    assert "capability_slot" not in keys
    assert _capability_slots(assembled) == set()
    compile_workflow(assembled, registries)
