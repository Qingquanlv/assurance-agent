from __future__ import annotations

import hashlib
from itertools import permutations
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.models import (
    CapabilityRegistry,
    EffectRegistry,
    PluginRequirement,
    ProductManifest,
    RegistrySet,
    ResourceEntry,
    ResourceRegistry,
    SchemaRegistry,
    SourceEntry,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRegistry,
    SourceRole,
    SourceSnapshot,
    WorkflowModuleRequirement,
)
from graph_engine.composition.workflow_assembler import (
    WorkflowAssemblyError,
    _load_authenticated_modules,
)
from graph_engine.graph import parse_workflow_module
from graph_engine.plugin_api import PluginDescriptor

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
) -> bytes:
    return f"""
schema_version: "1"
role: feature
owner_id: {owner_id}
module_id: {module_id}
module_version: {version}
exports:
  run:
    graph: run
    input_schema: {module_id}.run.input.v1
    output_schema: {module_id}.run.output.v1
    output_projection:
      type: child_output_pointer
      pointer: ""
capability_slots: {{}}
retry:
  once: {{max_attempts: 1}}
timeout:
  short: {{run_seconds: 30}}
graphs:
  run:
    max_activations: 2
    start: done
    nodes:
      done: {{kind: end}}
    edges: []
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
) -> ResourceEntry:
    return _resource(
        resource_id or f"{module_id}.module",
        owner_id,
        _feature_yaml(owner_id, module_id, version=version),
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


def _manifest(
    *requirements: WorkflowModuleRequirement,
) -> ProductManifest:
    return ProductManifest.model_validate(
        {
            "schema_version": "1",
            "source": None,
            "product_id": "toy.product",
            "product_version": "1.0.0",
            "engine_api": "2.0",
            "plugins": (PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),),
            "entrypoints": {"main": "root"},
            "workflow_module": parse_workflow_module(PRODUCT_MODULE),
            "workflow_module_resources": requirements,
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


def _registries(*resources: ResourceEntry) -> RegistrySet:
    sources: dict[SourceKey, SourceEntry] = {}
    for resource in resources:
        key = SourceKey(SourceRole.CONFIG, resource.owner_id)
        if key in sources:
            continue
        snapshot = _plugin_source(resource.owner_id)
        sources[key] = SourceEntry(source_key=key, snapshot=snapshot)
    return RegistrySet(
        sources=SourceRegistry(sources),
        capabilities=CapabilityRegistry.empty(),
        schemas=SchemaRegistry(entries={}),
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
