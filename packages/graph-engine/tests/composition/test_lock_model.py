from __future__ import annotations

import hashlib
import base64
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_engine.composition import (
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    ExecutionHostLock,
    InvocationLock,
    LockedDependency,
    LockedPlugin,
    LockedProduct,
    LockedSource,
    LockedSourceFile,
    RegistryDigests,
    RegistryProjections,
    SourceKey,
    SourceRole,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_F = "f" * 64


def _empty_contribution(plugin_id: str, source_digest: str) -> dict[str, object]:
    return {
        "owner_id": plugin_id,
        "source_key": {"role": "plugin", "owner_id": plugin_id},
        "source_digest": source_digest,
        "task_handlers": [],
        "commit_validators": [],
        "schemas": [],
        "resources": [],
        "effects": [],
        "bindings": [],
    }


def _lock(*, reverse_manifest: bool = False) -> InvocationLock:
    product_expectation = ProviderSource(
        distribution="toy-a",
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name="toy.a",
        entrypoint_value="toy_a.product:provider",
        declaration_path="toy_a/product-declaration.json",
        import_roots=("",),
    )
    manifest_items = [
        ("schema_version", "1"),
        ("source", product_expectation.model_dump(mode="json")),
        ("product_id", "toy.a"),
        ("product_version", "1.0.0"),
        ("engine_api", "1.0"),
        ("plugins", [{"plugin_id": "toy.runtime", "version_specifier": "==2.0.0"}]),
        ("entrypoints", {"你好": "根"}),
        ("configuration", {}),
        ("config_plugin_paths", []),
        ("workflow", None),
        ("workflow_resource_id", "toy.workflow"),
    ]
    manifest = dict(reversed(manifest_items) if reverse_manifest else manifest_items)
    product_source = LockedSource(
        kind="wheel_product",
        identity={
            "distribution": "toy-a",
            "version": "1.0.0",
            "entrypoint_group": "graph_engine.products",
            "entrypoint_name": "toy.a",
            "entrypoint_value": "toy_a.product:provider",
            "declaration_path": "toy_a/product-declaration.json",
            "import_roots": [""],
        },
        digest=_A,
        files=(LockedSourceFile(path="toy_a/product.py", sha256=_B),),
    )
    plugin_source = LockedSource(
        kind="wheel_plugin",
        identity={
            "distribution": "toy-runtime",
            "version": "2.0.0",
            "entrypoint_group": "graph_engine.plugins",
            "entrypoint_name": "toy.runtime",
            "entrypoint_value": "toy_runtime.plugin:provider",
            "declaration_path": "toy_runtime/plugin-declaration.json",
            "import_roots": [""],
        },
        digest=_C,
        files=(LockedSourceFile(path="toy_runtime/plugin.py", sha256=_D),),
    )
    engine_source = LockedSource(
        kind="engine",
        identity={
            "distribution": "graph-engine",
            "version": "1.0.0",
            "installation": "installed",
        },
        digest=_F,
        files=(),
    )
    product = LockedProduct(
        product_id="toy.a",
        product_version="1.0.0",
        manifest=manifest,
        manifest_digest=hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        source=product_source,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=ProviderSource(
            distribution="toy-runtime",
            version="2.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="toy.runtime",
            entrypoint_value="toy_runtime.plugin:provider",
            declaration_path="toy_runtime/plugin-declaration.json",
            import_roots=("",),
        ),
        plugin_id="toy.runtime",
        plugin_version="2.0.0",
        engine_api="1.0",
        dependencies=(),
        task_handlers=("toy.runtime.greet",),
        commit_validators=(),
        resources=("toy.runtime.prompt",),
        bindings=("toy.runtime.alias",),
    )
    prompt = b"prompt"
    prompt_digest = hashlib.sha256(prompt).hexdigest()
    task_provenance = ExecutableProvenance.create(
        kind=ExecutableKind.TASK_HANDLER,
        registry_id="toy.runtime.greet",
        owner_id="toy.runtime",
        source_key=SourceKey(SourceRole.PLUGIN, "toy.runtime"),
        source_digest=_C,
        module=ExecutableModuleProvenance(
            module_name="toy_runtime.plugin",
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="toy_runtime/plugin.py",
            authenticated_locations=(),
            physical_sha256=_D,
            source_digest=_C,
        ),
        callable_path="toy_runtime.plugin:Handler.execute",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )
    contribution = {
        **_empty_contribution("toy.runtime", _C),
        "task_handlers": [
            {
                "capability_id": "toy.runtime.greet",
                "implementation": task_provenance.projection(),
                "implementation_digest": task_provenance.digest,
            }
        ],
        "resources": [
            {
                "resource_id": "toy.runtime.prompt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(prompt).decode("ascii"),
                "content_sha256": prompt_digest,
            }
        ],
        "bindings": [
            {
                "capability_id": "toy.runtime.alias",
                "target_capability_id": "toy.runtime.greet",
                "data": {"locale": "zh-CN"},
                "resource_ids": ["toy.runtime.prompt"],
            }
        ],
    }
    plugin = LockedPlugin(
        plugin_id="toy.runtime",
        plugin_version="2.0.0",
        descriptor=descriptor,
        descriptor_digest=canonical_digest(descriptor.model_dump(mode="json")),
        contribution=contribution,
        contribution_digest=canonical_digest(contribution),
        dependencies=(),
        source=plugin_source,
    )
    registry_projections = RegistryProjections(
        sources=[
            {
                "source_key": {"role": "engine", "owner_id": "graph.engine"},
                "digest": _F,
            }
        ],
        capabilities=[
            {
                "capability_id": "toy.runtime.alias",
                "data": {"locale": "zh-CN"},
                "implementation_digest": task_provenance.digest,
                "kind": "binding",
                "owner_id": "toy.runtime",
                "resource_ids": ["toy.runtime.prompt"],
                "target_capability_id": "toy.runtime.greet",
                "target_implementation": task_provenance.projection(),
            },
            {
                "capability_id": "toy.runtime.greet",
                "implementation": task_provenance.projection(),
                "implementation_digest": task_provenance.digest,
                "kind": "task_handler",
                "owner_id": "toy.runtime",
            },
        ],
        schemas=[],
        resources=[
            {
                "resource_id": "toy.runtime.prompt",
                "owner_id": "toy.runtime",
                "media_type": "text/plain",
                "sha256": prompt_digest,
            }
        ],
        effects=[],
    )
    registry_digests = RegistryDigests(
        sources=canonical_digest(
            [
                {
                    "source_key": {"role": "engine", "owner_id": "graph.engine"},
                    "digest": _F,
                }
            ]
        ),
        capabilities=canonical_digest(
            [
                {
                    "capability_id": "toy.runtime.alias",
                    "data": {"locale": "zh-CN"},
                    "implementation_digest": task_provenance.digest,
                    "kind": "binding",
                    "owner_id": "toy.runtime",
                    "resource_ids": ["toy.runtime.prompt"],
                    "target_capability_id": "toy.runtime.greet",
                    "target_implementation": task_provenance.projection(),
                },
                {
                    "capability_id": "toy.runtime.greet",
                    "implementation": task_provenance.projection(),
                    "implementation_digest": task_provenance.digest,
                    "kind": "task_handler",
                    "owner_id": "toy.runtime",
                },
            ]
        ),
        schemas=canonical_digest([]),
        resources=canonical_digest(
            [
                {
                    "resource_id": "toy.runtime.prompt",
                    "owner_id": "toy.runtime",
                    "media_type": "text/plain",
                    "sha256": prompt_digest,
                }
            ]
        ),
        effects=canonical_digest([]),
    )
    configuration = {"toy.runtime": {"greeting": "你好"}}
    capability_bindings = [
        {
            "capability_id": "toy.runtime.alias",
            "data": {"locale": "zh-CN"},
            "implementation_digest": task_provenance.digest,
            "kind": "binding",
            "owner_id": "toy.runtime",
            "resource_ids": ["toy.runtime.prompt"],
            "target_capability_id": "toy.runtime.greet",
            "target_implementation": task_provenance.projection(),
        }
    ]
    compiled_workflow = {
        "entrypoints": {"你好": "根"},
        "graphs": {"根": {"start": "node"}},
        "name": "toy",
    }
    return InvocationLock.create(
        engine_api="1.0",
        engine=engine_source,
        engine_digest=_F,
        product=product,
        plugins=(plugin,),
        dependency_order=("toy.runtime",),
        registry_projections=registry_projections,
        registry_digests=registry_digests,
        configuration=configuration,
        configuration_digest=canonical_digest(configuration),
        capability_bindings=capability_bindings,
        capability_bindings_digest=canonical_digest(capability_bindings),
        compiled_workflow=compiled_workflow,
        compiled_workflow_digest=canonical_digest(compiled_workflow),
    )


def _recreate_lock(
    lock: InvocationLock,
    *,
    registry_projections: RegistryProjections | None = None,
    registry_digests: RegistryDigests | None = None,
    configuration: object | None = None,
    configuration_digest: str | None = None,
    capability_bindings: object | None = None,
    capability_bindings_digest: str | None = None,
    compiled_workflow: object | None = None,
    compiled_workflow_digest: str | None = None,
    product: LockedProduct | None = None,
    plugins: tuple[LockedPlugin, ...] | None = None,
    dependency_order: tuple[str, ...] | None = None,
) -> InvocationLock:
    return InvocationLock.create(
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product if product is None else product,
        plugins=lock.plugins if plugins is None else plugins,
        dependency_order=lock.dependency_order if dependency_order is None else dependency_order,
        registry_projections=registry_projections or lock.registry_projections,
        registry_digests=registry_digests or lock.registry_digests,
        configuration=lock.configuration if configuration is None else configuration,
        configuration_digest=configuration_digest or lock.configuration_digest,
        capability_bindings=(
            lock.capability_bindings if capability_bindings is None else capability_bindings
        ),
        capability_bindings_digest=(capability_bindings_digest or lock.capability_bindings_digest),
        compiled_workflow=(lock.compiled_workflow if compiled_workflow is None else compiled_workflow),
        compiled_workflow_digest=(compiled_workflow_digest or lock.compiled_workflow_digest),
    )


def test_invocation_lock_has_one_golden_canonical_projection() -> None:
    lock = _lock()

    expected = (
        Path(__file__)
        .with_name("invocation-lock-v2.golden.json")
        .read_text(encoding="utf-8")
        .strip()
        .encode()
    )
    assert lock.canonical_bytes == expected
    assert lock.digest == hashlib.sha256(expected).hexdigest()
    assert b'"canonical_bytes"' not in lock.canonical_bytes
    assert "digest" not in json.loads(lock.canonical_bytes)


def test_invocation_lock_schema_version_2_pins_execution_host() -> None:
    lock = _lock()
    assert lock.schema_version == "2"
    assert lock.execution_host.implementation_id == "graph.engine.task-host"
    assert lock.execution_host.wire_schema_version == "1"
    assert b'"execution_host"' in lock.canonical_bytes
    assert b'"schema_version":"2"' in lock.canonical_bytes
    payload = json.loads(lock.canonical_bytes)
    assert "execution_host" in payload
    assert payload["execution_host"]["implementation_id"] == "graph.engine.task-host"


def test_invocation_lock_rejects_schema_version_1_and_foreign_host() -> None:
    lock = _lock()
    with pytest.raises(ValidationError):
        InvocationLock(
            **{**lock.model_dump(exclude={"canonical_bytes", "digest"}), "schema_version": "1"},
            canonical_bytes=lock.canonical_bytes,
            digest=lock.digest,
        )
    drifted = ExecutionHostLock(
        implementation_id="graph.engine.other-host",
        implementation_digest=lock.execution_host.implementation_digest,
        wire_schema_version="1",
    )
    with pytest.raises((ValidationError, TypeError, ValueError), match="execution host"):
        InvocationLock.create(
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
            compiled_workflow=lock.compiled_workflow,
            compiled_workflow_digest=lock.compiled_workflow_digest,
            execution_host=drifted,
        )


def test_lock_projection_normalizes_unicode_map_and_tuple_order() -> None:
    first = _lock()
    second = _lock(reverse_manifest=True)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.dependency_order == ("toy.runtime",)
    assert b'["toy.runtime"]' in first.canonical_bytes
    assert "你好".encode() in first.canonical_bytes
    assert b"\\u4f60" not in first.canonical_bytes


@pytest.mark.parametrize("bad_float", (math.nan, math.inf, -math.inf))
def test_lock_values_reject_non_finite_floats(bad_float: float) -> None:
    with pytest.raises((ValidationError, ValueError), match="finite"):
        LockedProduct(
            product_id="toy.a",
            product_version="1.0.0",
            manifest={"ratio": bad_float},
            manifest_digest=_A,
            source=LockedSource(
                kind="product_file",
                identity={"product_id": "toy.a"},
                digest=_B,
                files=(),
            ),
        )


def test_invocation_lock_rejects_noncanonical_bytes_and_digest() -> None:
    lock = _lock()

    with pytest.raises(ValidationError, match="canonical bytes"):
        InvocationLock(
            **lock.model_dump(exclude={"canonical_bytes", "digest"}),
            canonical_bytes=b"{}",
            digest=hashlib.sha256(b"{}").hexdigest(),
        )


def test_invocation_lock_recomputes_exact_dependency_closure() -> None:
    lock = _lock()
    selected = lock.plugins[0]
    descriptor = selected.descriptor.model_copy(
        update={"dependencies": (PluginDependency("toy.missing", ">=1"),)}
    )
    descriptor_projection = descriptor.model_dump(mode="json")
    invalid = LockedPlugin(
        plugin_id=selected.plugin_id,
        plugin_version=selected.plugin_version,
        descriptor=descriptor,
        descriptor_digest=canonical_digest(descriptor_projection),
        contribution=selected.contribution,
        contribution_digest=selected.contribution_digest,
        dependencies=(LockedDependency(plugin_id="toy.missing", version_specifier=">=1"),),
        source=selected.source,
    )

    with pytest.raises(ValidationError, match="dependency declarations"):
        _recreate_lock(lock, plugins=(invalid,))
    with pytest.raises(ValidationError, match="digest"):
        InvocationLock(
            **lock.model_dump(exclude={"canonical_bytes", "digest"}),
            canonical_bytes=lock.canonical_bytes,
            digest=_A,
        )


def _locked_plugin(
    plugin_id: str,
    version: str,
    dependencies: tuple[PluginDependency, ...] = (),
) -> LockedPlugin:
    distribution = plugin_id.replace(".", "-")
    declaration_path = f"{plugin_id.replace('.', '_')}/plugin-declaration.json"
    source_expectation = ProviderSource(
        distribution=distribution,
        version=version,
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=plugin_id,
        entrypoint_value=f"{plugin_id.replace('.', '_')}.plugin:provider",
        declaration_path=declaration_path,
        import_roots=("",),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=source_expectation,
        plugin_id=plugin_id,
        plugin_version=version,
        engine_api="1.0",
        dependencies=dependencies,
        task_handlers=(),
        commit_validators=(),
    )
    return LockedPlugin(
        plugin_id=plugin_id,
        plugin_version=version,
        descriptor=descriptor,
        descriptor_digest=canonical_digest(descriptor.model_dump(mode="json")),
        contribution=_empty_contribution(plugin_id, _C),
        contribution_digest=canonical_digest(_empty_contribution(plugin_id, _C)),
        dependencies=tuple(
            LockedDependency(
                plugin_id=dependency.plugin_id,
                version_specifier=dependency.version_specifier,
            )
            for dependency in dependencies
        ),
        source=LockedSource(
            kind="wheel_plugin",
            identity={
                **source_expectation.model_dump(mode="json"),
                "plugin_id": plugin_id,
                "plugin_version": version,
            },
            digest=_C,
            files=(),
        ),
    )


def _product_with_requirements(
    product: LockedProduct,
    requirements: list[dict[str, str]],
) -> LockedProduct:
    manifest = product.model_dump(mode="json")["manifest"]
    assert isinstance(manifest, dict)
    manifest["plugins"] = requirements
    return LockedProduct(
        product_id=product.product_id,
        product_version=product.product_version,
        manifest=manifest,
        manifest_digest=canonical_digest(manifest),
        source=product.source,
    )


def test_invocation_lock_rejects_cycle_version_mismatch_and_noncanonical_order() -> None:
    lock = _lock()
    cyclic = _locked_plugin(
        "toy.runtime",
        "2.0.0",
        (PluginDependency("toy.runtime", ">=1"),),
    )
    with pytest.raises(ValidationError, match="dependency declarations"):
        _recreate_lock(lock, plugins=(cyclic,))

    base = _locked_plugin("toy.base", "1.0.0")
    incompatible = _locked_plugin(
        "toy.runtime",
        "2.0.0",
        (PluginDependency("toy.base", ">=2"),),
    )
    with pytest.raises(ValidationError, match="dependency declarations"):
        _recreate_lock(
            lock,
            plugins=(base, incompatible),
            dependency_order=("toy.base", "toy.runtime"),
        )

    product = _product_with_requirements(
        lock.product,
        [
            {"plugin_id": "toy.runtime", "version_specifier": "==2.0.0"},
            {"plugin_id": "toy.base", "version_specifier": "==1.0.0"},
        ],
    )
    with pytest.raises(ValidationError, match="not canonical"):
        _recreate_lock(
            lock,
            product=product,
            plugins=(base, lock.plugins[0]),
            dependency_order=("toy.runtime", "toy.base"),
        )


def test_invocation_lock_recomputes_product_root_version_constraints() -> None:
    lock = _lock()
    forged_product = _product_with_requirements(
        lock.product,
        [{"plugin_id": "toy.runtime", "version_specifier": "==9.9.9"}],
    )

    with pytest.raises(ValidationError, match="dependency declarations"):
        _recreate_lock(lock, product=forged_product)


def test_locked_plugin_authenticates_id_and_version_against_source_identity() -> None:
    source = LockedSource(
        kind="wheel_plugin",
        identity={
            "distribution": "toy-runtime",
            "version": "2.0.0",
            "entrypoint_group": "graph_engine.plugins",
            "entrypoint_name": "toy.runtime",
            "entrypoint_value": "toy_runtime.plugin:provider",
            "declaration_path": "toy_runtime/plugin-declaration.json",
            "import_roots": [""],
        },
        digest=_A,
        files=(),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=ProviderSource(
            distribution="toy-runtime",
            version="2.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="toy.runtime",
            entrypoint_value="toy_runtime.plugin:provider",
            declaration_path="toy_runtime/plugin-declaration.json",
            import_roots=("",),
        ),
        plugin_id="toy.runtime",
        plugin_version="2.0.0",
        engine_api="1.0",
        task_handlers=(),
        commit_validators=(),
    )
    descriptor_digest = canonical_digest(descriptor.model_dump(mode="json"))

    with pytest.raises(ValidationError, match="descriptor|source identity"):
        LockedPlugin(
            plugin_id="toy.other",
            plugin_version="2.0.0",
            descriptor=descriptor,
            descriptor_digest=descriptor_digest,
            contribution=_empty_contribution("toy.other", _A),
            contribution_digest=canonical_digest(_empty_contribution("toy.other", _A)),
            dependencies=(),
            source=source,
        )
    with pytest.raises(ValidationError, match="descriptor|source identity"):
        LockedPlugin(
            plugin_id="toy.runtime",
            plugin_version="3.0.0",
            descriptor=descriptor,
            descriptor_digest=descriptor_digest,
            contribution=_empty_contribution("toy.runtime", _A),
            contribution_digest=canonical_digest(_empty_contribution("toy.runtime", _A)),
            dependencies=(),
            source=source,
        )


def test_locked_plugin_contribution_authority_binds_the_typed_source_key() -> None:
    plugin = _lock().plugins[0]
    contribution = thaw_json(plugin.contribution)
    assert isinstance(contribution, dict)
    contribution["source_key"] = {"role": "config", "owner_id": plugin.plugin_id}

    with pytest.raises(ValidationError, match="contribution (source key|provenance)"):
        LockedPlugin(
            **plugin.model_dump(exclude={"contribution", "contribution_digest"}),
            contribution=contribution,
            contribution_digest=canonical_digest(contribution),
        )


def test_invocation_lock_rejects_self_consistent_missing_binding_target() -> None:
    lock = _lock()
    plugin = lock.plugins[0]
    descriptor = plugin.descriptor.model_copy(update={"task_handlers": ()})
    contribution = thaw_json(plugin.contribution)
    assert isinstance(contribution, dict)
    contribution["task_handlers"] = []
    forged_plugin = LockedPlugin(
        plugin_id=plugin.plugin_id,
        plugin_version=plugin.plugin_version,
        descriptor=descriptor,
        descriptor_digest=canonical_digest(descriptor.model_dump(mode="json")),
        contribution=contribution,
        contribution_digest=canonical_digest(contribution),
        dependencies=plugin.dependencies,
        source=plugin.source,
    )
    capabilities = [
        entry
        for entry in thaw_json(lock.registry_projections.capabilities)
        if isinstance(entry, dict) and entry.get("kind") == "binding"
    ]
    projections = RegistryProjections(
        sources=thaw_json(lock.registry_projections.sources),
        capabilities=capabilities,
        schemas=thaw_json(lock.registry_projections.schemas),
        resources=thaw_json(lock.registry_projections.resources),
        effects=thaw_json(lock.registry_projections.effects),
    )
    digests = lock.registry_digests.model_copy(update={"capabilities": canonical_digest(capabilities)})

    with pytest.raises(ValidationError, match="unknown target capability"):
        _recreate_lock(
            lock,
            plugins=(forged_plugin,),
            registry_projections=projections,
            registry_digests=digests,
        )


def test_invocation_lock_rejects_self_consistent_blank_resource_media_type() -> None:
    lock = _lock()
    plugin = lock.plugins[0]
    contribution = thaw_json(plugin.contribution)
    assert isinstance(contribution, dict)
    resources = contribution["resources"]
    assert isinstance(resources, list) and isinstance(resources[0], dict)
    resources[0]["media_type"] = ""
    with pytest.raises(ValidationError, match="resource.*media type"):
        LockedPlugin(
            plugin_id=plugin.plugin_id,
            plugin_version=plugin.plugin_version,
            descriptor=plugin.descriptor,
            descriptor_digest=plugin.descriptor_digest,
            contribution=contribution,
            contribution_digest=canonical_digest(contribution),
            dependencies=plugin.dependencies,
            source=plugin.source,
        )


def test_lock_authenticates_auditable_projections_against_their_digests() -> None:
    lock = _lock()

    with pytest.raises(ValidationError, match="registry digests"):
        _recreate_lock(
            lock,
            registry_digests=lock.registry_digests.model_copy(update={"sources": _A}),
        )
    with pytest.raises(ValidationError, match="configuration digest"):
        _recreate_lock(lock, configuration={"toy.runtime": {"greeting": "changed"}})
    with pytest.raises(ValidationError, match="capability bindings disagree"):
        _recreate_lock(
            lock,
            capability_bindings=[],
            capability_bindings_digest=canonical_digest([]),
        )
    with pytest.raises(ValidationError, match="workflow digest"):
        _recreate_lock(lock, compiled_workflow={"entrypoints": {}, "graphs": {}, "name": "toy"})


def test_lock_rejects_an_extra_unsupported_executable_projection_kind() -> None:
    lock = _lock()
    capabilities = [
        *thaw_json(lock.registry_projections.capabilities),
        {
            "capability_id": "toy.runtime.unknown",
            "kind": "unknown-executable",
            "owner_id": "toy.runtime",
        },
    ]
    projections = RegistryProjections(
        sources=lock.registry_projections.sources,
        capabilities=capabilities,
        schemas=lock.registry_projections.schemas,
        resources=lock.registry_projections.resources,
        effects=lock.registry_projections.effects,
    )
    digests = lock.registry_digests.model_copy(update={"capabilities": canonical_digest(capabilities)})

    with pytest.raises(ValidationError, match="contribution authority"):
        _recreate_lock(
            lock,
            registry_projections=projections,
            registry_digests=digests,
        )
