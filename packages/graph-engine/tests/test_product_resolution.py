import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import get_type_hints

import pytest
from pydantic import ValidationError

import graph_engine
from graph_engine.graph.compiler import CompileError
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.product import (
    PluginRequirement,
    ProductManifest,
    ProductResolutionError,
    load_plugin_entrypoint,
    load_product_entrypoint,
    resolve_product,
)


class _PingHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"pong": True})


@dataclass(frozen=True)
class _PluginProvider:
    plugin_id: str

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=None,
            plugin_id=self.plugin_id,
            plugin_version="1.0.0",
            engine_api="1.0",
            task_handlers=(f"{self.plugin_id}.ping",),
            commit_validators=(),
        )

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        del ports
        return PluginContribution(
            task_handlers={f"{self.plugin_id}.ping": _PingHandler()},
        )


@dataclass(frozen=True)
class _ProductProvider:
    def manifest(self) -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=None,
            product_id="toy.product",
            product_version="1.0.0",
            engine_api="1.0",
            plugins=(PluginRequirement(plugin_id="toy.one", version_specifier="==1.0.0"),),
            entrypoints={"main": "root"},
            configuration={},
            workflow=WorkflowDef.model_validate(
                {
                    "name": "toy",
                    "entrypoints": {"main": "root"},
                    "retry": {"once": {"max_attempts": 1}},
                    "timeout": {"short": {"run_seconds": 5}},
                    "graphs": {
                        "root": {
                            "max_activations": 2,
                            "start": "ping",
                            "nodes": {
                                "ping": {
                                    "kind": "task",
                                    "capability": "toy.one.ping",
                                    "retry": "once",
                                    "timeout": "short",
                                },
                                "done": {"kind": "end"},
                            },
                            "edges": [{"from": "ping", "to": "done"}],
                        }
                    },
                }
            ),
        )


@dataclass
class _ChangingProductProvider:
    manifests: tuple[ProductManifest, ...]
    calls: int = 0

    def manifest(self) -> ProductManifest:
        manifest = self.manifests[min(self.calls, len(self.manifests) - 1)]
        self.calls += 1
        return manifest


@dataclass
class _ChangingPluginProvider:
    descriptors: tuple[PluginDescriptor, ...]
    descriptor_calls: int = 0
    contribution_calls: int = 0

    def descriptor(self) -> PluginDescriptor:
        descriptor = self.descriptors[min(self.descriptor_calls, len(self.descriptors) - 1)]
        self.descriptor_calls += 1
        return descriptor

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        self.contribution_calls += 1
        descriptor = self.descriptors[0]
        assert ports == RegistryPorts(engine_api="1.0")
        return PluginContribution(
            task_handlers={capability_id: _PingHandler() for capability_id in descriptor.task_handlers},
        )


class _SingleLookupMap(Mapping[str, PluginProvider]):
    def __init__(self, provider: PluginProvider) -> None:
        self.provider = provider
        self.lookups = 0

    def __getitem__(self, key: str) -> PluginProvider:
        if key != "toy.one":
            raise KeyError(key)
        self.lookups += 1
        if self.lookups > 1:
            raise AssertionError("selected plugin was looked up more than once")
        return self.provider

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("available plugins must not be enumerated")

    def __len__(self) -> int:
        return 1


def _descriptor(
    *,
    plugin_id: str = "toy.one",
    version: str = "1.0.0",
    engine_api: str = "1.0",
    task_handlers: tuple[str, ...] = ("toy.one.ping",),
) -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version=version,
        engine_api=engine_api,
        task_handlers=task_handlers,
        commit_validators=(),
    )


@pytest.fixture
def product_provider() -> _ProductProvider:
    return _ProductProvider()


@pytest.fixture
def plugin_one() -> _PluginProvider:
    return _PluginProvider("toy.one")


@pytest.fixture
def plugin_two() -> _PluginProvider:
    return _PluginProvider("toy.two")


def test_only_manifest_plugins_are_enabled(product_provider, plugin_one, plugin_two) -> None:
    resolved = resolve_product(product_provider, {"toy.one": plugin_one, "toy.two": plugin_two})
    assert set(resolved.registry.task_handlers) == {"toy.one.ping"}


def test_installed_but_unlisted_plugin_cannot_change_product(
    product_provider, plugin_one, plugin_two
) -> None:
    without_extra = resolve_product(product_provider, {"toy.one": plugin_one})
    with_extra = resolve_product(product_provider, {"toy.one": plugin_one, "toy.two": plugin_two})
    assert without_extra.digest == with_extra.digest


def test_missing_explicit_plugin_fails(product_provider) -> None:
    with pytest.raises(ProductResolutionError, match="missing plugin toy.one==1.0.0"):
        resolve_product(product_provider, {})


def test_manifest_is_read_exactly_once(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    valid = product_provider.manifest()
    incompatible = valid.model_copy(update={"engine_api": "2.0"})
    changing = _ChangingProductProvider((valid, incompatible))

    resolved = resolve_product(changing, {"toy.one": plugin_one})

    assert resolved.manifest is valid
    assert changing.calls == 1


def test_selected_provider_and_custom_map_are_each_read_once(
    product_provider: _ProductProvider,
) -> None:
    provider = _ChangingPluginProvider((_descriptor(), _descriptor(version="9.0.0")))
    available = _SingleLookupMap(provider)

    resolved = resolve_product(product_provider, available)

    assert set(resolved.registry.task_handlers) == {"toy.one.ping"}
    assert available.lookups == 1
    assert provider.descriptor_calls == 1
    assert provider.contribution_calls == 1


def test_unlisted_provider_is_never_observed(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    class PoisonProvider:
        def descriptor(self) -> PluginDescriptor:
            raise AssertionError("unlisted provider was observed")

        def contribute(self, ports: RegistryPorts) -> PluginContribution:
            raise AssertionError(f"unlisted provider contributed with {ports}")

    resolved = resolve_product(
        product_provider,
        {"toy.one": plugin_one, "toy.unlisted": PoisonProvider()},
    )
    assert set(resolved.registry.task_handlers) == {"toy.one.ping"}


@pytest.mark.parametrize(
    ("descriptor", "message"),
    [
        (_descriptor(version="1.0.1"), "version '1.0.1'; expected '==1.0.0'"),
        (_descriptor(engine_api="1.0.0"), "engine API '1.0.0'; expected '1.0'"),
        (_descriptor(plugin_id="toy.other"), "for toy.one described toy.other"),
    ],
)
def test_selected_plugin_descriptor_must_match_exactly_before_contribution(
    product_provider: _ProductProvider,
    descriptor: PluginDescriptor,
    message: str,
) -> None:
    plugin = _ChangingPluginProvider((descriptor,))
    with pytest.raises(ProductResolutionError, match=message):
        resolve_product(product_provider, {"toy.one": plugin})
    assert plugin.contribution_calls == 0


def test_product_engine_api_must_match_exactly_before_plugin_lookup(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    incompatible = product_provider.manifest().model_copy(update={"engine_api": "1.0.0"})
    changing = _ChangingProductProvider((incompatible,))
    available = _SingleLookupMap(plugin_one)

    with pytest.raises(ProductResolutionError, match="engine API '1.0.0'; expected '1.0'"):
        resolve_product(changing, available)
    assert available.lookups == 0


def test_each_resolution_assembles_a_fresh_registry(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    first = resolve_product(product_provider, {"toy.one": plugin_one})
    second = resolve_product(product_provider, {"toy.one": plugin_one})
    assert first.registry is not second.registry
    assert first.registry.task_handlers is not second.registry.task_handlers


def test_resolution_compiles_the_manifest_workflow(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    resolved = resolve_product(product_provider, {"toy.one": plugin_one})
    assert resolved.workflow.graphs["root"].nodes["ping"].definition.capability == "toy.one.ping"

    manifest = product_provider.manifest()
    ping = (
        manifest.workflow.graphs["root"].nodes["ping"].model_copy(update={"capability": "toy.missing.ping"})
    )
    graph = manifest.workflow.graphs["root"].model_copy(
        update={"nodes": {**manifest.workflow.graphs["root"].nodes, "ping": ping}}
    )
    workflow = manifest.workflow.model_copy(update={"graphs": {"root": graph}})
    invalid = _ChangingProductProvider((manifest.model_copy(update={"workflow": workflow}),))
    with pytest.raises(CompileError, match="unknown capability toy.missing.ping"):
        resolve_product(invalid, {"toy.one": plugin_one})


def test_digest_is_deterministic_and_covers_manifest_descriptor_and_compilation(
    product_provider: _ProductProvider,
) -> None:
    base_descriptor = _descriptor()
    base = resolve_product(
        product_provider,
        {"toy.one": _ChangingPluginProvider((base_descriptor,))},
    )
    repeated = resolve_product(
        product_provider,
        {"toy.one": _ChangingPluginProvider((base_descriptor,))},
    )
    assert base.digest == repeated.digest

    manifest = product_provider.manifest()
    changed_manifest = manifest.model_copy(update={"product_version": "1.0.1"})
    manifest_result = resolve_product(
        _ChangingProductProvider((changed_manifest,)),
        {"toy.one": _ChangingPluginProvider((base_descriptor,))},
    )
    assert base.digest != manifest_result.digest

    expanded_descriptor = _descriptor(task_handlers=("toy.one.ping", "toy.one.extra"))
    descriptor_result = resolve_product(
        product_provider,
        {"toy.one": _ChangingPluginProvider((expanded_descriptor,))},
    )
    assert base.digest != descriptor_result.digest

    graph = manifest.workflow.graphs["root"].model_copy(update={"max_activations": 3})
    workflow = manifest.workflow.model_copy(update={"graphs": {"root": graph}})
    compiled_result = resolve_product(
        _ChangingProductProvider((manifest.model_copy(update={"workflow": workflow}),)),
        {"toy.one": _ChangingPluginProvider((base_descriptor,))},
    )
    assert base.workflow.digest != compiled_result.workflow.digest
    assert base.digest != compiled_result.digest


def test_resolved_product_is_frozen_and_package_exports_product_api(
    product_provider: _ProductProvider, plugin_one: _PluginProvider
) -> None:
    resolved = resolve_product(product_provider, {"toy.one": plugin_one})
    with pytest.raises(FrozenInstanceError):
        resolved.digest = "changed"  # type: ignore[reportAttributeAccessIssue]
    assert graph_engine.resolve_product is resolve_product
    assert graph_engine.ProductManifest is ProductManifest


def test_resolved_product_registry_has_an_honest_public_legacy_view(
    product_provider: _ProductProvider,
    plugin_one: _PluginProvider,
) -> None:
    resolved = resolve_product(product_provider, {"toy.one": plugin_one})
    registry_type = getattr(graph_engine, "LegacyResolvedCapabilityView", None)

    assert registry_type is not None
    assert get_type_hints(graph_engine.ResolvedProduct)["registry"] is registry_type
    assert isinstance(resolved.registry, registry_type)
    assert tuple(resolved.registry.task_handlers) == ("toy.one.ping",)
    assert resolved.registry.commit_validators == {}
    assert not hasattr(resolved.registry, "entries")
    assert not hasattr(resolved.registry, "bindings")
    with pytest.raises(TypeError):
        resolved.registry.task_handlers["toy.one.other"] = _PingHandler()


def test_legacy_resolved_binding_preserves_alias_request_semantics(
    product_provider: _ProductProvider,
) -> None:
    requests: list[TaskRequest] = []

    class RecordingHandler:
        async def execute(self, request: TaskRequest, _context: TaskContext) -> TaskOutcome:
            requests.append(request)
            return TaskOutcome.succeeded()

    class BindingProvider:
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id="toy.one",
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=("toy.one.ping",),
                commit_validators=(),
                resources=("toy.one.prompt",),
                bindings=("toy.one.alias",),
            )

        def contribute(self, _ports: RegistryPorts) -> PluginContribution:
            return PluginContribution(
                task_handlers={"toy.one.ping": RecordingHandler()},
                resources=(ResourceContribution("toy.one.prompt", "text/plain", b"prompt"),),
                bindings=(
                    CapabilityBindingContribution(
                        capability_id="toy.one.alias",
                        target_capability_id="toy.one.ping",
                        data={"prompt": {"mode": "strict"}},
                        resource_ids=("toy.one.prompt",),
                    ),
                ),
            )

    manifest = product_provider.manifest()
    assert manifest.workflow is not None
    ping = manifest.workflow.graphs["root"].nodes["ping"].model_copy(update={"capability": "toy.one.alias"})
    graph = manifest.workflow.graphs["root"].model_copy(
        update={"nodes": {**manifest.workflow.graphs["root"].nodes, "ping": ping}}
    )
    workflow = manifest.workflow.model_copy(update={"graphs": {"root": graph}})
    resolved = resolve_product(
        _ChangingProductProvider((manifest.model_copy(update={"workflow": workflow}),)),
        {"toy.one": BindingProvider()},
    )
    alias = resolved.registry.task_handlers["toy.one.alias"]
    context = TaskContext(workspace_root=Path("/workspace"), heartbeat=lambda: None)
    wrong = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="ping",
        capability_id="toy.one.ping",
        attempt=1,
        input={},
    )

    with pytest.raises(ValueError, match="received request"):
        asyncio.run(alias.execute(wrong, context))

    correct = wrong.model_copy(update={"capability_id": "toy.one.alias"})
    asyncio.run(alias.execute(correct, context))
    assert requests[0].capability_id == "toy.one.alias"
    assert requests[0].target_capability_id == "toy.one.ping"
    assert requests[0].binding_data == {"prompt": {"mode": "strict"}}
    assert requests[0].resource_ids == ("toy.one.prompt",)
    with pytest.raises(TypeError):
        requests[0].binding_data["prompt"]["mode"] = "changed"  # type: ignore[index]


def test_legacy_resolved_binding_rejects_an_unknown_resource(
    product_provider: _ProductProvider,
) -> None:
    class BindingProvider:
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id="toy.one",
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=("toy.one.ping",),
                commit_validators=(),
                bindings=("toy.one.alias",),
            )

        def contribute(self, _ports: RegistryPorts) -> PluginContribution:
            return PluginContribution(
                task_handlers={"toy.one.ping": _PingHandler()},
                bindings=(
                    CapabilityBindingContribution(
                        capability_id="toy.one.alias",
                        target_capability_id="toy.one.ping",
                        resource_ids=("toy.one.missing",),
                    ),
                ),
            )

    with pytest.raises(ProductResolutionError, match="unknown legacy binding resource"):
        resolve_product(product_provider, {"toy.one": BindingProvider()})


class _EffectHandler:
    async def apply(self, _intent: EffectIntent, _key: str) -> EffectApplyResult:
        return EffectApplyResult.applied({"ok": True})

    async def reconcile(self, _intent: EffectIntent, _key: str) -> EffectReconcileResult:
        return EffectReconcileResult.applied({"ok": True})


@pytest.mark.parametrize(
    ("descriptor", "contribution", "message"),
    [
        (
            _descriptor(task_handlers=("toy.other.ping",)),
            PluginContribution(task_handlers={"toy.other.ping": _PingHandler()}),
            "task handler id is not owned by toy.one",
        ),
        (
            _descriptor(),
            PluginContribution(task_handlers={"toy.one.ping": object()}),
            "task handler has no execute method",
        ),
        (
            PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id="toy.one",
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=("toy.one.ping",),
                commit_validators=("toy.one.validate",),
            ),
            PluginContribution(
                task_handlers={"toy.one.ping": _PingHandler()},
                commit_validators={"toy.one.validate": object()},
            ),
            "commit validator has no validate method",
        ),
        (
            PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id="toy.one",
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=("toy.one.ping",),
                commit_validators=(),
                effects=("toy.one.audit",),
            ),
            PluginContribution(
                task_handlers={"toy.one.ping": _PingHandler()},
                effects=(
                    EffectRegistration(
                        "toy.one.audit",
                        "toy.one.missing-intent",
                        "toy.one.missing-receipt",
                        _EffectHandler(),
                        EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
                    ),
                ),
            ),
            "unknown effect intent schema",
        ),
        (
            PluginDescriptor(
                schema_version="1",
                source=None,
                plugin_id="toy.one",
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=("toy.one.ping",),
                commit_validators=(),
                schemas=("toy.one.intent", "toy.one.receipt"),
                effects=("toy.one.audit",),
            ),
            PluginContribution(
                task_handlers={"toy.one.ping": _PingHandler()},
                schemas=(
                    SchemaContribution("toy.one.intent", "application/schema+json", b"{}"),
                    SchemaContribution("toy.one.receipt", "application/schema+json", b"{}"),
                ),
                effects=(
                    EffectRegistration(
                        "toy.one.audit",
                        "toy.one.intent",
                        "toy.one.receipt",
                        object(),
                        EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
                    ),
                ),
            ),
            "effect handler must provide apply and reconcile",
        ),
    ],
)
def test_phase_one_private_bridge_uses_closed_shared_contribution_validation(
    product_provider: _ProductProvider,
    descriptor: PluginDescriptor,
    contribution: PluginContribution,
    message: str,
) -> None:
    class MalformedProvider:
        def descriptor(self) -> PluginDescriptor:
            return descriptor

        def contribute(self, _ports: RegistryPorts) -> PluginContribution:
            return contribution

    with pytest.raises(ProductResolutionError, match=message):
        resolve_product(product_provider, {"toy.one": MalformedProvider()})


@pytest.mark.parametrize("product_id", ["toy", "Toy.product", "toy_product.main", "toy/product"])
def test_product_manifest_rejects_invalid_product_ids(
    product_provider: _ProductProvider, product_id: str
) -> None:
    manifest = product_provider.manifest()
    with pytest.raises(ValidationError, match="product id"):
        ProductManifest.model_validate(
            {
                **manifest.model_dump(mode="python", by_alias=True, exclude_unset=True),
                "product_id": product_id,
            }
        )


@pytest.mark.parametrize("plugin_id", ["toy", "Toy.one", "toy_one.plugin", "toy/one"])
def test_plugin_requirement_rejects_invalid_plugin_ids(plugin_id: str) -> None:
    with pytest.raises(ValidationError, match="plugin requirement id"):
        PluginRequirement(plugin_id=plugin_id, version_specifier="==1.0.0")


def test_product_manifest_requires_a_nonempty_unique_plugin_list(
    product_provider: _ProductProvider,
) -> None:
    manifest = product_provider.manifest()
    payload = manifest.model_dump(mode="python", by_alias=True, exclude_unset=True)
    with pytest.raises(ValidationError, match="at least one plugin"):
        ProductManifest.model_validate({**payload, "plugins": []})
    with pytest.raises(ValidationError, match="requirements must be unique"):
        ProductManifest.model_validate({**payload, "plugins": [payload["plugins"][0], payload["plugins"][0]]})


def test_product_models_are_frozen_and_forbid_extra(
    product_provider: _ProductProvider,
) -> None:
    manifest = product_provider.manifest()
    with pytest.raises(ValidationError, match="frozen"):
        manifest.product_version = "2.0.0"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PluginRequirement.model_validate(
            {"plugin_id": "toy.one", "version_specifier": "==1.0.0", "optional": True}
        )


@dataclass(frozen=True)
class _EntryPoint:
    name: str
    value: object
    loaded_names: list[str]
    load_error: Exception | None = None

    def load(self) -> object:
        self.loaded_names.append(self.name)
        if self.load_error is not None:
            raise self.load_error
        return self.value


def test_entrypoint_loading_searches_only_product_group_and_loads_only_the_match(
    monkeypatch: pytest.MonkeyPatch, product_provider: _ProductProvider
) -> None:
    groups: list[str] = []
    loaded_names: list[str] = []
    selected = _EntryPoint("toy.product", product_provider, loaded_names)
    unselected = _EntryPoint("toy.other", object(), loaded_names)

    def entry_points(*, group: str) -> tuple[_EntryPoint, ...]:
        groups.append(group)
        if group == "graph_engine.products":
            return (unselected, selected)
        raise AssertionError(f"unexpected entry-point group: {group}")

    monkeypatch.setattr("graph_engine.product.metadata.entry_points", entry_points)

    loaded = load_product_entrypoint("toy.product")

    assert loaded is product_provider
    assert groups == ["graph_engine.products"]
    assert loaded_names == ["toy.product"]


def test_missing_product_entrypoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: ())
    with pytest.raises(ProductResolutionError, match="no product entry point for toy.product"):
        load_product_entrypoint("toy.product")


def test_multiple_product_entrypoints_fail_without_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_names: list[str] = []
    matches = (
        _EntryPoint("toy.product", object(), loaded_names),
        _EntryPoint("toy.product", object(), loaded_names),
    )
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: matches)
    with pytest.raises(ProductResolutionError, match="multiple product entry points for toy.product"):
        load_product_entrypoint("toy.product")
    assert loaded_names == []


def test_product_entrypoint_requires_callable_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    entry_point = _EntryPoint("toy.product", object(), [])
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: (entry_point,))
    with pytest.raises(ProductResolutionError, match="does not provide callable manifest"):
        load_product_entrypoint("toy.product")


def test_product_entrypoint_load_failure_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = ImportError("missing trusted wheel dependency")
    entry_point = _EntryPoint("toy.product", object(), [], load_error=failure)
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: (entry_point,))

    with pytest.raises(
        ProductResolutionError,
        match="cannot load product entry point toy.product",
    ) as raised:
        load_product_entrypoint("toy.product")

    assert raised.value.__cause__ is failure


def test_product_entrypoint_rejects_invalid_id_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_discovery(*, group: str) -> tuple[_EntryPoint, ...]:
        raise AssertionError(f"discovered entry points from {group}")

    monkeypatch.setattr("graph_engine.product.metadata.entry_points", unexpected_discovery)
    with pytest.raises(ProductResolutionError, match="invalid product id"):
        load_product_entrypoint("toy")


def test_plugin_entrypoint_loading_searches_only_plugin_group_and_loads_only_the_match(
    monkeypatch: pytest.MonkeyPatch, plugin_one: _PluginProvider
) -> None:
    groups: list[str] = []
    loaded_names: list[str] = []
    selected = _EntryPoint("toy-a", plugin_one, loaded_names)
    unselected = _EntryPoint("toy-b", object(), loaded_names)

    def entry_points(*, group: str) -> tuple[_EntryPoint, ...]:
        groups.append(group)
        if group == "graph_engine.plugins":
            return (unselected, selected)
        raise AssertionError(f"unexpected entry-point group: {group}")

    monkeypatch.setattr("graph_engine.product.metadata.entry_points", entry_points)

    loaded = load_plugin_entrypoint("toy-a")

    assert loaded is plugin_one
    assert groups == ["graph_engine.plugins"]
    assert loaded_names == ["toy-a"]


def test_missing_plugin_entrypoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: ())
    with pytest.raises(ProductResolutionError, match="no plugin entry point for toy-a"):
        load_plugin_entrypoint("toy-a")


def test_multiple_plugin_entrypoints_fail_without_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_names: list[str] = []
    matches = (
        _EntryPoint("toy-a", object(), loaded_names),
        _EntryPoint("toy-a", object(), loaded_names),
    )
    monkeypatch.setattr("graph_engine.product.metadata.entry_points", lambda *, group: matches)

    with pytest.raises(ProductResolutionError, match="multiple plugin entry points for toy-a"):
        load_plugin_entrypoint("toy-a")

    assert loaded_names == []
