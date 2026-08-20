from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from graph_engine.composition import (
    CapabilityBindingEntry,
    CapabilityRegistry,
    CommitValidatorEntry,
    EffectRegistry,
    RegistryConflict,
    RegistrySet,
    ResourceRegistry,
    SchemaEntry,
    SchemaRegistry,
    SourceEntry,
    SourceIdentity,
    SourceKind,
    SourceRegistry,
    SourceSnapshot,
    TaskHandlerEntry,
    build_registries,
)
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    CandidateWriteSet,
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginContribution,
    PluginContractError,
    PluginDescriptor,
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
    ValidationResult,
    validate_contribution,
)


class _Handler:
    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []

    async def execute(self, request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        self.requests.append(request)
        return TaskOutcome.succeeded({"ok": True})


class _Validator:
    def validate(
        self,
        _candidate: CandidateWriteSet,
        _context: ValidationContext,
    ) -> ValidationResult:
        return ValidationResult(accepted=True)


class _EffectHandler:
    async def apply(self, _intent: EffectIntent, _idempotency_key: str) -> EffectApplyResult:
        return EffectApplyResult.applied({"ok": True})

    async def reconcile(
        self,
        _intent: EffectIntent,
        _idempotency_key: str,
    ) -> EffectReconcileResult:
        return EffectReconcileResult.applied({"ok": True})


def _source(plugin_id: str, *, kind: SourceKind = SourceKind.CONFIG_TREE) -> SourceSnapshot:
    if kind == SourceKind.CONFIG_TREE:
        identity = SourceIdentity(
            kind=kind,
            root=Path(f"/sources/{plugin_id}"),
            plugin_id=plugin_id,
            plugin_version="1.0.0",
        )
    else:
        identity = SourceIdentity(
            kind=kind,
            root=Path(f"/sources/{plugin_id}"),
            distribution=plugin_id.replace(".", "-"),
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
        )
    return SourceSnapshot.from_identity(identity, ())


def _runtime_contribution(handler: _Handler | None = None) -> PluginContribution:
    return PluginContribution(
        task_handlers={"toy.runtime.execute": handler or _Handler()},
        commit_validators={"toy.runtime.validate": _Validator()},
        schemas=(
            SchemaContribution(
                "toy.runtime.intent",
                "application/schema+json",
                b'{"type":"object"}',
            ),
            SchemaContribution(
                "toy.runtime.receipt",
                "application/schema+json",
                b'{"type":"object"}',
            ),
        ),
        resources=(ResourceContribution("toy.runtime.prompt", "text/plain", b"hello"),),
        effects=(
            EffectRegistration(
                kind="toy.runtime.audit",
                intent_schema_id="toy.runtime.intent",
                receipt_schema_id="toy.runtime.receipt",
                handler=_EffectHandler(),
                policy=EffectPolicy(max_attempts=2, timeout_seconds=3, backoff_seconds=0),
            ),
        ),
    )


def _binding_contribution(
    capability_id: str,
    target_capability_id: str,
    *,
    resource_ids: tuple[str, ...] = (),
    data: object = None,
) -> PluginContribution:
    return PluginContribution(
        resources=(ResourceContribution("toy.flow.prompt", "text/plain", b"hello"),)
        if "toy.flow.prompt" in resource_ids
        else (),
        bindings=(
            CapabilityBindingContribution(
                capability_id=capability_id,
                target_capability_id=target_capability_id,
                data=data,
                resource_ids=resource_ids,
            ),
        ),
    )


def test_registry_builder_freezes_all_five_views() -> None:
    registries = build_registries(
        sources=(_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
        contributions=(_runtime_contribution(),),
        dependency_order=("toy.runtime",),
    )

    assert tuple(registries.sources.entries) == ("toy.runtime",)
    assert tuple(registries.capabilities.task_handlers) == ("toy.runtime.execute",)
    assert tuple(registries.schemas.entries) == (
        "toy.runtime.intent",
        "toy.runtime.receipt",
    )
    assert tuple(registries.resources.entries) == ("toy.runtime.prompt",)
    assert tuple(registries.effects.entries) == ("toy.runtime.audit",)

    mappings = (
        registries.sources.entries,
        registries.capabilities.task_handlers,
        registries.schemas.entries,
        registries.resources.entries,
        registries.effects.entries,
    )
    for mapping in mappings:
        with pytest.raises(TypeError):
            mapping["toy.other"] = object()  # type: ignore[index,assignment]

    with pytest.raises(FrozenInstanceError):
        registries.effects = registries.effects  # type: ignore[misc]


def test_public_registry_views_reject_inconsistent_entry_mappings() -> None:
    handler = _Handler()
    validator = _Validator()
    handler_entry = TaskHandlerEntry("toy.runtime.execute", "toy.runtime", handler)
    validator_entry = CommitValidatorEntry("toy.runtime.validate", "toy.runtime", validator)

    with pytest.raises(ValueError, match="task handler view disagrees"):
        CapabilityRegistry(
            entries={
                handler_entry.capability_id: handler_entry,
                validator_entry.capability_id: validator_entry,
            },
            task_handlers={validator_entry.capability_id: handler},
            commit_validators={validator_entry.capability_id: validator},
            bindings={},
        )

    source = _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN)
    with pytest.raises(ValueError, match="source registry key disagrees"):
        SourceRegistry({"toy.other": SourceEntry("toy.runtime", source)})


def test_public_registry_rejects_value_equal_unselected_binding_projection() -> None:
    target = _Handler()
    sources = (
        _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),
        _source("toy.flow"),
    )
    contributions = (
        _runtime_contribution(target),
        _binding_contribution(
            "toy.flow.run",
            "toy.runtime.execute",
            data={"steps": ["one", "two"]},
        ),
    )
    first = build_registries(sources, contributions, ("toy.runtime", "toy.flow"))
    second = build_registries(sources, contributions, ("toy.runtime", "toy.flow"))
    selected = first.capabilities
    unselected_binding = second.capabilities.bindings["toy.flow.run"]

    assert selected.bindings["toy.flow.run"] == unselected_binding
    assert selected.bindings["toy.flow.run"] is not unselected_binding
    with pytest.raises(ValueError, match="binding view disagrees"):
        CapabilityRegistry(
            entries=selected.entries,
            task_handlers=selected.task_handlers,
            commit_validators=selected.commit_validators,
            bindings={"toy.flow.run": unselected_binding},
        )


def test_public_binding_entry_rejects_untrusted_handler() -> None:
    with pytest.raises(TypeError, match="engine-derived"):
        CapabilityBindingEntry(
            capability_id="toy.flow.run",
            owner_id="toy.flow",
            target_capability_id="toy.runtime.execute",
            data={"steps": ["one", "two"]},
            resource_ids=("toy.flow.prompt",),
            handler=_Handler(),
        )


def test_public_source_entry_authenticates_its_snapshot_identity() -> None:
    with pytest.raises(ValueError, match="source id disagrees with snapshot identity"):
        SourceEntry(
            source_id="toy.fake",
            snapshot=_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),
        )


def test_registry_set_rejects_executable_owner_that_is_not_a_plugin_source() -> None:
    product_snapshot = SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PRODUCT,
            root=Path("/sources/toy.product"),
            distribution="toy-product",
            version="1.0.0",
            entrypoint_group="graph_engine.products",
            entrypoint_name="toy.product",
        ),
        (),
    )
    handler = _Handler()
    handler_entry = TaskHandlerEntry("toy.product.execute", "toy.product", handler)

    with pytest.raises(ValueError, match="owner is not a plugin source"):
        RegistrySet(
            sources=SourceRegistry(
                {"toy.product.product-source": SourceEntry("toy.product.product-source", product_snapshot)}
            ),
            capabilities=CapabilityRegistry(
                entries={handler_entry.capability_id: handler_entry},
                task_handlers={handler_entry.capability_id: handler},
                commit_validators={},
                bindings={},
            ),
            schemas=SchemaRegistry({}),
            resources=ResourceRegistry({}),
            effects=EffectRegistry({}),
        )


def test_public_schema_entry_authenticates_json_and_derived_dialect() -> None:
    invalid = b"not-json"
    with pytest.raises(ValueError, match="valid JSON"):
        SchemaEntry(
            schema_id="toy.runtime.invalid",
            owner_id="toy.runtime",
            media_type="application/schema+json",
            content=invalid,
            sha256=hashlib.sha256(invalid).hexdigest(),
            dialect=None,
        )

    schema = b'{"$schema":"https://json-schema.org/draft/2020-12/schema"}'
    with pytest.raises(ValueError, match="dialect disagrees"):
        SchemaEntry(
            schema_id="toy.runtime.schema",
            owner_id="toy.runtime",
            media_type="application/schema+json",
            content=schema,
            sha256=hashlib.sha256(schema).hexdigest(),
            dialect=None,
        )


def test_binding_must_target_a_selected_task_handler() -> None:
    with pytest.raises(RegistryConflict, match="unknown target capability"):
        build_registries(
            sources=(_source("toy.flow"),),
            contributions=(_binding_contribution("toy.flow.run", "missing.execute"),),
            dependency_order=("toy.flow",),
        )


def test_binding_adapter_preserves_alias_target_frozen_data_and_resource_ids() -> None:
    target = _Handler()
    registries = build_registries(
        sources=(
            _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),
            _source("toy.flow"),
        ),
        contributions=(
            _runtime_contribution(target),
            _binding_contribution(
                "toy.flow.run",
                "toy.runtime.execute",
                resource_ids=("toy.flow.prompt",),
                data={"skill": {"name": "greet"}, "steps": ["one", "two"]},
            ),
        ),
        dependency_order=("toy.runtime", "toy.flow"),
    )
    request = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="toy.flow.run",
        attempt=1,
        input={"name": "Ada"},
    )

    outcome = asyncio.run(
        registries.capabilities.task_handlers["toy.flow.run"].execute(
            request,
            TaskContext(workspace_root=Path("/workspace"), heartbeat=lambda: None),
        )
    )

    assert outcome.status == "succeeded"
    bound = target.requests[0]
    assert bound.capability_id == "toy.flow.run"
    assert bound.target_capability_id == "toy.runtime.execute"
    assert bound.binding_data == {
        "skill": {"name": "greet"},
        "steps": ("one", "two"),
    }
    assert bound.resource_ids == ("toy.flow.prompt",)
    assert bound.model_dump(mode="json")["binding_data"] == {
        "skill": {"name": "greet"},
        "steps": ["one", "two"],
    }
    with pytest.raises(TypeError):
        bound.binding_data["skill"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        bound.binding_data["skill"]["name"] = "changed"  # type: ignore[index]


def test_registry_rejects_duplicate_ids_within_one_kind() -> None:
    duplicate = PluginContribution(
        schemas=(
            SchemaContribution("toy.runtime.schema", "application/schema+json", b"{}"),
            SchemaContribution("toy.runtime.schema", "application/schema+json", b"{}"),
        )
    )
    with pytest.raises(RegistryConflict, match="duplicate schema id"):
        build_registries(
            (_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
            (duplicate,),
            ("toy.runtime",),
        )


@pytest.mark.parametrize("direct_kind", ["task handler", "commit validator"])
@pytest.mark.parametrize(
    "dependency_order",
    [
        ("toy.flow", "toy.flow.runtime"),
        ("toy.flow.runtime", "toy.flow"),
    ],
)
def test_binding_collision_with_direct_capability_is_order_independent(
    direct_kind: str,
    dependency_order: tuple[str, str],
) -> None:
    shared_id = "toy.flow.runtime.shared"
    direct = (
        PluginContribution(
            task_handlers={
                "toy.flow.runtime.target": _Handler(),
                shared_id: _Handler(),
            }
        )
        if direct_kind == "task handler"
        else PluginContribution(
            task_handlers={"toy.flow.runtime.target": _Handler()},
            commit_validators={shared_id: _Validator()},
        )
    )
    contributions = {
        "toy.flow": _binding_contribution(
            shared_id,
            "toy.flow.runtime.target",
        ),
        "toy.flow.runtime": direct,
    }

    with pytest.raises(RegistryConflict, match=f"cross-kind registry id: {shared_id}"):
        build_registries(
            sources=tuple(_source(plugin_id, kind=SourceKind.WHEEL_PLUGIN) for plugin_id in dependency_order),
            contributions=tuple(contributions[plugin_id] for plugin_id in dependency_order),
            dependency_order=dependency_order,
        )


def test_registry_rejects_cross_kind_ids() -> None:
    contribution = PluginContribution(
        task_handlers={"toy.runtime.shared": _Handler()},
        resources=(ResourceContribution("toy.runtime.shared", "text/plain", b"x"),),
    )
    with pytest.raises(RegistryConflict, match="cross-kind registry id"):
        build_registries(
            (_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
            (contribution,),
            ("toy.runtime",),
        )


def test_descriptor_contribution_disagreement_remains_a_contract_error() -> None:
    descriptor = PluginDescriptor(
        "toy.runtime",
        "1.0.0",
        "1.0",
        ("toy.runtime.execute",),
        (),
    )
    with pytest.raises(PluginContractError, match="task handler declarations disagree"):
        validate_contribution(descriptor, PluginContribution.empty())


def test_registry_rejects_ids_not_owned_by_the_contributing_plugin() -> None:
    contribution = PluginContribution(
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.other.run",
                target_capability_id="toy.runtime.execute",
            ),
        )
    )
    with pytest.raises(RegistryConflict, match="binding id is not owned by toy.flow"):
        build_registries(
            (_source("toy.flow"),),
            (contribution,),
            ("toy.flow",),
        )


def test_registry_rejects_alias_cycles_before_target_resolution() -> None:
    contribution = PluginContribution(
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.flow.first",
                target_capability_id="toy.flow.second",
            ),
            CapabilityBindingContribution(
                capability_id="toy.flow.second",
                target_capability_id="toy.flow.first",
            ),
        )
    )
    with pytest.raises(RegistryConflict, match="alias cycle"):
        build_registries(
            (_source("toy.flow"),),
            (contribution,),
            ("toy.flow",),
        )


def test_registry_rejects_dangling_binding_resources() -> None:
    with pytest.raises(RegistryConflict, match="unknown binding resource"):
        build_registries(
            sources=(
                _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),
                _source("toy.flow"),
            ),
            contributions=(
                _runtime_contribution(),
                _binding_contribution(
                    "toy.flow.run",
                    "toy.runtime.execute",
                    resource_ids=("toy.flow.missing",),
                ),
            ),
            dependency_order=("toy.runtime", "toy.flow"),
        )


@pytest.mark.parametrize(
    ("intent_schema_id", "receipt_schema_id", "message"),
    [
        ("toy.runtime.missing", "toy.runtime.receipt", "unknown effect intent schema"),
        ("toy.runtime.intent", "toy.runtime.missing", "unknown effect receipt schema"),
    ],
)
def test_registry_rejects_dangling_effect_schemas(
    intent_schema_id: str,
    receipt_schema_id: str,
    message: str,
) -> None:
    contribution = PluginContribution(
        schemas=(
            SchemaContribution("toy.runtime.intent", "application/schema+json", b"{}"),
            SchemaContribution("toy.runtime.receipt", "application/schema+json", b"{}"),
        ),
        effects=(
            EffectRegistration(
                "toy.runtime.audit",
                intent_schema_id,
                receipt_schema_id,
                _EffectHandler(),
                EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
            ),
        ),
    )
    with pytest.raises(RegistryConflict, match=message):
        build_registries(
            (_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
            (contribution,),
            ("toy.runtime",),
        )


def test_schema_entries_require_schema_media_and_valid_json() -> None:
    for schema in (
        SchemaContribution("toy.runtime.schema", "application/json", b"{}"),
        SchemaContribution("toy.runtime.schema", "application/schema+json", b"not-json"),
    ):
        with pytest.raises(RegistryConflict, match="schema"):
            build_registries(
                (_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
                (PluginContribution(schemas=(schema,)),),
                ("toy.runtime",),
            )


def test_registry_mappings_have_canonical_key_order() -> None:
    contribution = PluginContribution(
        task_handlers={
            "toy.runtime.zed": _Handler(),
            "toy.runtime.alpha": _Handler(),
        },
        resources=(
            ResourceContribution("toy.runtime.zed-resource", "text/plain", b"z"),
            ResourceContribution("toy.runtime.alpha-resource", "text/plain", b"a"),
        ),
    )
    registries = build_registries(
        (_source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),),
        (contribution,),
        ("toy.runtime",),
    )
    assert tuple(registries.capabilities.task_handlers) == (
        "toy.runtime.alpha",
        "toy.runtime.zed",
    )
    assert tuple(registries.resources.entries) == (
        "toy.runtime.alpha-resource",
        "toy.runtime.zed-resource",
    )


def test_config_sources_cannot_contribute_executable_handlers() -> None:
    with pytest.raises(RegistryConflict, match="config source cannot contribute executable"):
        build_registries(
            (_source("toy.flow"),),
            (PluginContribution(task_handlers={"toy.flow.run": _Handler()}),),
            ("toy.flow",),
        )


def test_registry_requires_exact_selected_source_and_contribution_sets() -> None:
    with pytest.raises(RegistryConflict, match="selected plugin sources disagree"):
        build_registries(
            (_source("toy.other"),),
            (PluginContribution.empty(),),
            ("toy.flow",),
        )
    with pytest.raises(RegistryConflict, match="contribution count"):
        build_registries(
            (_source("toy.flow"),),
            (),
            ("toy.flow",),
        )


def test_plugin_contribution_has_no_sixth_registry_kind() -> None:
    with pytest.raises(TypeError):
        PluginContribution(lifecycles=())  # type: ignore[call-arg]
