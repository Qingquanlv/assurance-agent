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
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    RegistryConflict,
    RegistrySet,
    ResourceRegistry,
    SchemaEntry,
    SchemaRegistry,
    SourceEntry,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceRegistry,
    SourceSnapshot,
    TaskHandlerEntry,
)
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ContributionAuthority,
    ExecutableAuthority,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import (
    _build_registries as _build_authenticated_registries,
    validate_registry_contribution_authorities,
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
from graph_engine.frozen_json import thaw_json


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
            entrypoint_value=f"{plugin_id.replace('.', '_')}:provider",
            declaration_path=f"{plugin_id.replace('.', '_')}/plugin-declaration.json",
            import_roots=("",),
            plugin_id=plugin_id,
            plugin_version="1.0.0",
        )
    return SourceSnapshot.from_identity(identity, ())


def _source_key(snapshot: SourceSnapshot) -> SourceKey:
    role = SourceRole.CONFIG if snapshot.identity.kind is SourceKind.CONFIG_TREE else SourceRole.PLUGIN
    owner_id = snapshot.identity.plugin_id
    assert owner_id is not None
    return SourceKey(role, owner_id)


def _proof(
    snapshot: SourceSnapshot,
    kind: ExecutableKind,
    registry_id: str,
) -> ExecutableProvenance:
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
        callable_path="implementation:Handler.slot",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )


def _authenticated(
    snapshot: SourceSnapshot,
    contribution: PluginContribution,
) -> AuthenticatedContribution:
    source_key = _source_key(snapshot)
    if source_key.role is SourceRole.CONFIG and (
        contribution.task_handlers or contribution.commit_validators or contribution.effects
    ):
        raise RegistryConflict("config source cannot contribute executable")
    proofs = [
        *(
            _proof(snapshot, ExecutableKind.TASK_HANDLER, registry_id)
            for registry_id in contribution.task_handlers
        ),
        *(
            _proof(snapshot, ExecutableKind.COMMIT_VALIDATOR, registry_id)
            for registry_id in contribution.commit_validators
        ),
        *(
            _proof(snapshot, kind, registration.kind)
            for registration in contribution.effects
            for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
        ),
    ]
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=source_key.owner_id,
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=tuple(contribution.task_handlers),
        commit_validators=tuple(contribution.commit_validators),
        schemas=tuple(item.schema_id for item in contribution.schemas),
        resources=tuple(item.resource_id for item in contribution.resources),
        effects=tuple(item.kind for item in contribution.effects),
        bindings=tuple(item.capability_id for item in contribution.bindings),
    )
    executable_objects = {
        **{
            (ExecutableKind.TASK_HANDLER, registry_id): executable
            for registry_id, executable in contribution.task_handlers.items()
        },
        **{
            (ExecutableKind.COMMIT_VALIDATOR, registry_id): executable
            for registry_id, executable in contribution.commit_validators.items()
        },
        **{
            (kind, registration.kind): registration.handler
            for registration in contribution.effects
            for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
        },
    }
    ordered_proofs = tuple(sorted(proofs, key=lambda item: (item.registry_id, item.kind.value)))
    authority_set = ContributionAuthority(
        provider_binding=object() if source_key.role is SourceRole.PLUGIN else None,
        descriptor=descriptor,
        owner_id=source_key.owner_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        contribution=contribution,
        authorities=tuple(
            ExecutableAuthority(
                executable=executable_objects[(proof.kind, proof.registry_id)],
                function=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[proof.kind.slot],
                bound_self=executable_objects[(proof.kind, proof.registry_id)],
                descriptor=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[
                    proof.kind.slot
                ],
                provenance=proof,
            )
            for proof in ordered_proofs
        ),
    )
    return AuthenticatedContribution(
        owner_id=source_key.owner_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=ordered_proofs,
        authority=authority_set,
    )


def build_registries(
    sources: tuple[SourceSnapshot, ...],
    contributions: tuple[PluginContribution, ...],
    dependency_order: tuple[str, ...],
) -> RegistrySet:
    by_owner = {_source_key(snapshot).owner_id: snapshot for snapshot in sources}
    if set(by_owner) != set(dependency_order):
        return _build_authenticated_registries(sources, (), dependency_order)
    if len(contributions) != len(dependency_order):
        return _build_authenticated_registries(sources, (), dependency_order)
    try:
        authenticated = tuple(
            _authenticated(by_owner[owner_id], contribution)
            for owner_id, contribution in zip(dependency_order, contributions, strict=True)
        )
    except (ValueError, PluginContractError) as error:
        if "config contribution" in str(error):
            raise RegistryConflict("config source cannot contribute executable") from error
        raise RegistryConflict(
            str(error).replace("cross-kind contribution id", "cross-kind registry id")
        ) from error
    return _build_authenticated_registries(sources, authenticated, dependency_order)


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

    assert tuple(registries.sources.entries) == (SourceKey(SourceRole.PLUGIN, "toy.runtime"),)
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
    source = _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN)
    authenticated = _authenticated(
        source,
        PluginContribution(
            task_handlers={"toy.runtime.execute": handler},
            commit_validators={"toy.runtime.validate": validator},
        ),
    )
    handler_entry = TaskHandlerEntry(
        "toy.runtime.execute",
        "toy.runtime",
        handler,
        _proof(source, ExecutableKind.TASK_HANDLER, "toy.runtime.execute"),
        authenticated.authority,
    )
    validator_entry = CommitValidatorEntry(
        "toy.runtime.validate",
        "toy.runtime",
        validator,
        _proof(source, ExecutableKind.COMMIT_VALIDATOR, "toy.runtime.validate"),
        authenticated.authority,
    )

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

    with pytest.raises(ValueError, match="source registry key disagrees"):
        SourceRegistry(
            {
                SourceKey(SourceRole.PLUGIN, "toy.other"): SourceEntry(
                    SourceKey(SourceRole.PLUGIN, "toy.runtime"), source
                )
            }
        )


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
    provenance = _proof(
        _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN),
        ExecutableKind.TASK_HANDLER,
        "toy.runtime.execute",
    )
    with pytest.raises(TypeError, match="engine-derived"):
        CapabilityBindingEntry(
            capability_id="toy.flow.run",
            owner_id="toy.flow",
            target_capability_id="toy.runtime.execute",
            data={"steps": ["one", "two"]},
            resource_ids=("toy.flow.prompt",),
            handler=_Handler(),
            target_provenance=provenance,
        )


def test_public_source_entry_authenticates_its_snapshot_identity() -> None:
    with pytest.raises(ValueError, match="source key disagrees with snapshot identity"):
        SourceEntry(
            source_key=SourceKey(SourceRole.PLUGIN, "toy.fake"),
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
            entrypoint_value="toy_product:provider",
            declaration_path="toy_product/product-declaration.json",
            import_roots=("",),
            product_id="toy.product",
            product_version="1.0.0",
        ),
        (),
    )
    handler = _Handler()
    plugin_source = _source("toy.product", kind=SourceKind.WHEEL_PLUGIN)
    authenticated = _authenticated(
        plugin_source,
        PluginContribution(task_handlers={"toy.product.execute": handler}),
    )
    handler_entry = TaskHandlerEntry(
        "toy.product.execute",
        "toy.product",
        handler,
        _proof(plugin_source, ExecutableKind.TASK_HANDLER, "toy.product.execute"),
        authenticated.authority,
    )

    with pytest.raises(ValueError, match="owner is not a plugin source"):
        RegistrySet(
            sources=SourceRegistry(
                {
                    SourceKey(SourceRole.PRODUCT, "toy.product"): SourceEntry(
                        SourceKey(SourceRole.PRODUCT, "toy.product"), product_snapshot
                    )
                }
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
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api="1.0",
        task_handlers=("toy.runtime.execute",),
        commit_validators=(),
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


def test_every_selected_plugin_retains_one_six_category_contribution_authority() -> None:
    contribution = PluginContribution(
        task_handlers={"toy.runtime.execute": _Handler()},
        commit_validators={"toy.runtime.validate": _Validator()},
        schemas=(
            SchemaContribution("toy.runtime.intent", "application/schema+json", b"{}"),
            SchemaContribution("toy.runtime.receipt", "application/schema+json", b"{}"),
        ),
        resources=(ResourceContribution("toy.runtime.prompt", "text/plain", b"prompt"),),
        effects=(
            EffectRegistration(
                "toy.runtime.audit",
                "toy.runtime.intent",
                "toy.runtime.receipt",
                _EffectHandler(),
                EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
            ),
        ),
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.runtime.bound",
                target_capability_id="toy.runtime.execute",
                data={"mode": "strict"},
                resource_ids=("toy.runtime.prompt",),
            ),
        ),
    )
    snapshot = _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN)
    authenticated = _authenticated(snapshot, contribution)
    registries = _build_authenticated_registries(
        (snapshot,),
        (authenticated,),
        ("toy.runtime",),
    )

    validate_registry_contribution_authorities(
        registries,
        {"toy.runtime": authenticated.authority},
        (authenticated.descriptor,),
    )
    assert authenticated.authority.contribution is contribution
    assert (
        authenticated.authority.projection["schemas"][0]["content_sha256"]
        == hashlib.sha256(b"{}").hexdigest()
    )
    assert (
        authenticated.authority.projection["resources"][0]["content_sha256"]
        == hashlib.sha256(b"prompt").hexdigest()
    )
    assert thaw_json(authenticated.authority.projection["bindings"]) == [
        {
            "capability_id": "toy.runtime.bound",
            "data": {"mode": "strict"},
            "resource_ids": ["toy.runtime.prompt"],
            "target_capability_id": "toy.runtime.execute",
        }
    ]


@pytest.mark.parametrize("registry_kind", ["schema", "resource", "binding"])
def test_contribution_authority_rejects_missing_nonexecutable_registry_value(
    registry_kind: str,
) -> None:
    contribution = PluginContribution(
        task_handlers={"toy.runtime.execute": _Handler()},
        schemas=(SchemaContribution("toy.runtime.schema", "application/schema+json", b"{}"),),
        resources=(ResourceContribution("toy.runtime.prompt", "text/plain", b"prompt"),),
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.runtime.bound",
                target_capability_id="toy.runtime.execute",
                resource_ids=("toy.runtime.prompt",),
            ),
        ),
    )
    snapshot = _source("toy.runtime", kind=SourceKind.WHEEL_PLUGIN)
    authenticated = _authenticated(snapshot, contribution)
    registries = _build_authenticated_registries(
        (snapshot,),
        (authenticated,),
        ("toy.runtime",),
    )
    if registry_kind == "schema":
        forged = RegistrySet(
            sources=registries.sources,
            capabilities=registries.capabilities,
            schemas=SchemaRegistry({}),
            resources=registries.resources,
            effects=registries.effects,
        )
    elif registry_kind == "resource":
        direct = registries.capabilities.entries["toy.runtime.execute"]
        assert isinstance(direct, TaskHandlerEntry)
        forged_capabilities = CapabilityRegistry(
            entries={"toy.runtime.execute": direct},
            task_handlers={"toy.runtime.execute": direct.handler},
            commit_validators={},
            bindings={},
        )
        forged = RegistrySet(
            sources=registries.sources,
            capabilities=forged_capabilities,
            schemas=registries.schemas,
            resources=ResourceRegistry({}),
            effects=registries.effects,
        )
    else:
        direct = registries.capabilities.entries["toy.runtime.execute"]
        assert isinstance(direct, TaskHandlerEntry)
        forged_capabilities = CapabilityRegistry(
            entries={"toy.runtime.execute": direct},
            task_handlers={"toy.runtime.execute": direct.handler},
            commit_validators={},
            bindings={},
        )
        forged = RegistrySet(
            sources=registries.sources,
            capabilities=forged_capabilities,
            schemas=registries.schemas,
            resources=registries.resources,
            effects=registries.effects,
        )

    with pytest.raises(ValueError, match="contribution authority"):
        validate_registry_contribution_authorities(
            forged,
            {"toy.runtime": authenticated.authority},
            (authenticated.descriptor,),
        )


def test_data_only_plugin_authority_is_required_even_without_executables() -> None:
    snapshot = _source("toy.data")
    authenticated = _authenticated(
        snapshot,
        PluginContribution(resources=(ResourceContribution("toy.data.prompt", "text/plain", b"prompt"),)),
    )
    registries = _build_authenticated_registries((snapshot,), (authenticated,), ("toy.data",))

    with pytest.raises(ValueError, match="selected plugin contribution authorities"):
        validate_registry_contribution_authorities(registries, {}, (authenticated.descriptor,))
