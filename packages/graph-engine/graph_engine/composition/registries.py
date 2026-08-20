from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from types import MappingProxyType

from graph_engine.composition.models import (
    AuthenticatedContribution,
    CapabilityBindingEntry,
    CapabilityRegistry,
    CommitValidatorEntry,
    EffectEntry,
    EffectRegistry,
    ExecutableKind,
    RegistrySet,
    ResourceEntry,
    ResourceRegistry,
    SchemaEntry,
    SchemaRegistry,
    SourceEntry,
    SourceKey,
    SourceKind,
    SourceRegistry,
    SourceSnapshot,
    TaskHandlerEntry,
    _snapshot_owner_id,
    _snapshot_source_key,
)
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import freeze_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    CommitValidator,
    EffectPolicy,
    EffectRegistration,
    PluginContribution,
    ResourceContribution,
    SchemaContribution,
    TaskHandler,
)


class RegistryConflict(GraphEngineError):
    """Raised when selected contributions cannot form the five closed registries."""


@dataclass(frozen=True, slots=True)
class _OwnedContribution:
    owner_id: str
    source: SourceEntry
    contribution: PluginContribution
    authenticated: AuthenticatedContribution


def _build_registries(
    sources: tuple[SourceSnapshot, ...],
    contributions: tuple[AuthenticatedContribution, ...],
    dependency_order: tuple[str, ...],
) -> RegistrySet:
    """Build the five closed, canonical, immutable registry views."""

    order = _validate_dependency_order(dependency_order)
    source_view, plugin_sources = _build_source_registry(sources, order)
    owned = _pair_contributions(contributions, order, plugin_sources)
    _validate_source_capabilities(owned)
    schema_view = _build_schema_registry(owned)
    resource_view = _build_resource_registry(owned)
    capability_view = _build_capability_registry(owned)
    effect_view = _build_effect_registry(owned, schema_view)
    _validate_cross_kind_ids(
        capability_view,
        schema_view,
        resource_view,
        effect_view,
    )
    _validate_bindings(capability_view, resource_view)
    return RegistrySet(
        sources=source_view,
        capabilities=capability_view,
        schemas=schema_view,
        resources=resource_view,
        effects=effect_view,
    )


def _validate_dependency_order(dependency_order: tuple[str, ...]) -> tuple[str, ...]:
    order = tuple(dependency_order)
    for plugin_id in order:
        _qualified_id(plugin_id, "dependency plugin id")
    duplicates = _duplicates(order)
    if duplicates:
        raise RegistryConflict(f"dependency order repeats plugin id: {duplicates[0]}")
    return order


def _build_source_registry(
    sources: tuple[SourceSnapshot, ...],
    dependency_order: tuple[str, ...],
) -> tuple[SourceRegistry, Mapping[str, SourceEntry]]:
    entries: dict[SourceKey, SourceEntry] = {}
    plugin_entries: dict[str, SourceEntry] = {}
    for snapshot in tuple(sources):
        if not isinstance(snapshot, SourceSnapshot):
            raise RegistryConflict("source registry accepts only SourceSnapshot entries")
        try:
            source_key = _snapshot_source_key(snapshot)
        except (TypeError, ValueError) as error:
            raise RegistryConflict(str(error)) from error
        if source_key in entries:
            raise RegistryConflict(f"duplicate source key: {source_key}")
        entry = SourceEntry(source_key=source_key, snapshot=snapshot)
        entries[source_key] = entry
        plugin_id = _plugin_source_id(snapshot)
        if plugin_id is not None:
            if plugin_id in plugin_entries:
                raise RegistryConflict(f"duplicate selected plugin source: {plugin_id}")
            plugin_entries[plugin_id] = entry

    selected = set(plugin_entries)
    expected = set(dependency_order)
    if selected != expected:
        missing = tuple(sorted(expected - selected))
        unexpected = tuple(sorted(selected - expected))
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise RegistryConflict("selected plugin sources disagree: " + "; ".join(details))
    return SourceRegistry(entries), MappingProxyType(dict(sorted(plugin_entries.items())))


def _plugin_source_id(snapshot: SourceSnapshot) -> str | None:
    if snapshot.identity.kind not in {
        SourceKind.CONFIG_TREE,
        SourceKind.WHEEL_PLUGIN,
        SourceKind.EDITABLE_PLUGIN,
    }:
        return None
    try:
        return _snapshot_owner_id(snapshot)
    except (TypeError, ValueError) as error:
        raise RegistryConflict(str(error)) from error


def _pair_contributions(
    contributions: tuple[AuthenticatedContribution, ...],
    dependency_order: tuple[str, ...],
    plugin_sources: Mapping[str, SourceEntry],
) -> tuple[_OwnedContribution, ...]:
    frozen = tuple(contributions)
    if len(frozen) != len(dependency_order):
        raise RegistryConflict(
            f"contribution count {len(frozen)} does not match dependency order count {len(dependency_order)}"
        )
    owned: list[_OwnedContribution] = []
    for owner_id, authenticated in zip(dependency_order, frozen, strict=True):
        if not isinstance(authenticated, AuthenticatedContribution):
            raise RegistryConflict("registry builder accepts only authenticated contributions")
        source = plugin_sources[owner_id]
        if (
            authenticated.owner_id != owner_id
            or authenticated.source_key != source.source_key
            or authenticated.source_digest != source.snapshot.digest
        ):
            raise RegistryConflict(f"authenticated contribution source disagrees: {owner_id}")
        owned.append(_OwnedContribution(owner_id, source, authenticated.contribution, authenticated))
    return tuple(owned)


def _validate_source_capabilities(contributions: tuple[_OwnedContribution, ...]) -> None:
    for owned in contributions:
        contribution = owned.contribution
        if owned.source.snapshot.identity.kind != SourceKind.CONFIG_TREE:
            continue
        if contribution.task_handlers or contribution.commit_validators or contribution.effects:
            raise RegistryConflict(f"config source cannot contribute executable capability: {owned.owner_id}")


def _build_schema_registry(contributions: tuple[_OwnedContribution, ...]) -> SchemaRegistry:
    entries: dict[str, SchemaEntry] = {}
    for owned in contributions:
        for schema in owned.contribution.schemas:
            if not isinstance(schema, SchemaContribution):
                raise RegistryConflict(f"plugin {owned.owner_id} contributed an invalid schema entry")
            _owned_id(schema.schema_id, owned.owner_id, "schema")
            if schema.schema_id in entries:
                raise RegistryConflict(f"duplicate schema id: {schema.schema_id}")
            if schema.media_type != "application/schema+json":
                raise RegistryConflict(
                    f"schema media type must be application/schema+json: {schema.schema_id}"
                )
            try:
                entries[schema.schema_id] = SchemaEntry.from_content(
                    schema_id=schema.schema_id,
                    owner_id=owned.owner_id,
                    media_type=schema.media_type,
                    content=schema.content,
                )
            except (TypeError, ValueError) as error:
                raise RegistryConflict(f"invalid schema content: {schema.schema_id}") from error
    return SchemaRegistry(entries)


def _build_resource_registry(contributions: tuple[_OwnedContribution, ...]) -> ResourceRegistry:
    entries: dict[str, ResourceEntry] = {}
    for owned in contributions:
        for resource in owned.contribution.resources:
            if not isinstance(resource, ResourceContribution):
                raise RegistryConflict(f"plugin {owned.owner_id} contributed an invalid resource entry")
            _owned_id(resource.resource_id, owned.owner_id, "resource")
            if resource.resource_id in entries:
                raise RegistryConflict(f"duplicate resource id: {resource.resource_id}")
            content = bytes(resource.content)
            entries[resource.resource_id] = ResourceEntry(
                resource_id=resource.resource_id,
                owner_id=owned.owner_id,
                media_type=resource.media_type,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
    return ResourceRegistry(entries)


def _build_capability_registry(
    contributions: tuple[_OwnedContribution, ...],
) -> CapabilityRegistry:
    entries: dict[str, TaskHandlerEntry | CommitValidatorEntry | CapabilityBindingEntry] = {}
    task_handlers: dict[str, TaskHandler] = {}
    task_handler_entries: dict[str, TaskHandlerEntry] = {}
    validators: dict[str, CommitValidator] = {}
    binding_contributions: dict[str, tuple[str, CapabilityBindingContribution]] = {}
    reservations: dict[str, str] = {}

    for owned in contributions:
        for capability_id, handler in owned.contribution.task_handlers.items():
            _owned_id(capability_id, owned.owner_id, "task handler")
            _reserve_capability(reservations, capability_id, "task handler")
            if not callable(getattr(handler, "execute", None)):
                raise RegistryConflict(f"task handler has no execute method: {capability_id}")
            entry = TaskHandlerEntry(
                capability_id,
                owned.owner_id,
                handler,
                owned.authenticated.executable(ExecutableKind.TASK_HANDLER, capability_id),
            )
            entries[capability_id] = entry
            task_handlers[capability_id] = handler
            task_handler_entries[capability_id] = entry
        for capability_id, validator in owned.contribution.commit_validators.items():
            _owned_id(capability_id, owned.owner_id, "commit validator")
            _reserve_capability(reservations, capability_id, "commit validator")
            if not callable(getattr(validator, "validate", None)):
                raise RegistryConflict(f"commit validator has no validate method: {capability_id}")
            entry = CommitValidatorEntry(
                capability_id,
                owned.owner_id,
                validator,
                owned.authenticated.executable(ExecutableKind.COMMIT_VALIDATOR, capability_id),
            )
            entries[capability_id] = entry
            validators[capability_id] = validator
        for binding in owned.contribution.bindings:
            if not isinstance(binding, CapabilityBindingContribution):
                raise RegistryConflict(f"plugin {owned.owner_id} contributed an invalid binding entry")
            _owned_id(binding.capability_id, owned.owner_id, "binding")
            _reserve_capability(reservations, binding.capability_id, "binding")
            if len(set(binding.resource_ids)) != len(binding.resource_ids):
                raise RegistryConflict(f"duplicate binding resource id: {binding.capability_id}")
            binding_contributions[binding.capability_id] = (owned.owner_id, binding)

    _reject_alias_cycles(binding_contributions)
    bindings: dict[str, CapabilityBindingEntry] = {}
    for capability_id in sorted(binding_contributions):
        owner_id, binding = binding_contributions[capability_id]
        target = task_handlers.get(binding.target_capability_id)
        target_entry = task_handler_entries.get(binding.target_capability_id)
        if target is None or target_entry is None:
            raise RegistryConflict(
                f"unknown target capability for binding {capability_id}: {binding.target_capability_id}"
            )
        entry = CapabilityBindingEntry._from_target(
            capability_id=capability_id,
            owner_id=owner_id,
            target_capability_id=binding.target_capability_id,
            data=_freeze_binding_data(binding.data),
            resource_ids=tuple(binding.resource_ids),
            target=target,
            target_provenance=target_entry.provenance,
        )
        entries[capability_id] = entry
        bindings[capability_id] = entry
        task_handlers[capability_id] = entry.handler

    return CapabilityRegistry(
        entries=entries,
        task_handlers=task_handlers,
        commit_validators=validators,
        bindings=bindings,
    )


def _reserve_capability(reservations: dict[str, str], capability_id: str, kind: str) -> None:
    previous = reservations.get(capability_id)
    if previous is None:
        reservations[capability_id] = kind
        return
    if previous == kind:
        raise RegistryConflict(f"duplicate {kind} id: {capability_id}")
    raise RegistryConflict(f"cross-kind registry id: {capability_id} is both {previous} and {kind}")


def _reject_alias_cycles(
    bindings: Mapping[str, tuple[str, CapabilityBindingContribution]],
) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(alias_id: str) -> None:
        marker = state.get(alias_id, 0)
        if marker == 2:
            return
        if marker == 1:
            start = stack.index(alias_id)
            cycle = (*stack[start:], alias_id)
            raise RegistryConflict(f"alias cycle: {' -> '.join(cycle)}")
        state[alias_id] = 1
        stack.append(alias_id)
        target_id = bindings[alias_id][1].target_capability_id
        if target_id in bindings:
            visit(target_id)
        stack.pop()
        state[alias_id] = 2

    for alias_id in sorted(bindings):
        visit(alias_id)


def _build_effect_registry(
    contributions: tuple[_OwnedContribution, ...],
    schemas: SchemaRegistry,
) -> EffectRegistry:
    entries: dict[str, EffectEntry] = {}
    for owned in contributions:
        for registration in owned.contribution.effects:
            if not isinstance(registration, EffectRegistration):
                raise RegistryConflict(f"plugin {owned.owner_id} contributed an invalid effect entry")
            _owned_id(registration.kind, owned.owner_id, "effect")
            if registration.kind in entries:
                raise RegistryConflict(f"duplicate effect id: {registration.kind}")
            if registration.intent_schema_id not in schemas.entries:
                raise RegistryConflict(
                    f"unknown effect intent schema for {registration.kind}: {registration.intent_schema_id}"
                )
            if registration.receipt_schema_id not in schemas.entries:
                raise RegistryConflict(
                    f"unknown effect receipt schema for {registration.kind}: {registration.receipt_schema_id}"
                )
            if not callable(getattr(registration.handler, "apply", None)) or not callable(
                getattr(registration.handler, "reconcile", None)
            ):
                raise RegistryConflict(
                    f"effect handler must provide apply and reconcile: {registration.kind}"
                )
            if not isinstance(registration.policy, EffectPolicy):
                raise RegistryConflict(f"effect policy is invalid: {registration.kind}")
            entries[registration.kind] = EffectEntry(
                kind=registration.kind,
                owner_id=owned.owner_id,
                intent_schema_id=registration.intent_schema_id,
                receipt_schema_id=registration.receipt_schema_id,
                handler=registration.handler,
                policy=registration.policy,
                apply_provenance=owned.authenticated.executable(
                    ExecutableKind.EFFECT_APPLY,
                    registration.kind,
                ),
                reconcile_provenance=owned.authenticated.executable(
                    ExecutableKind.EFFECT_RECONCILE,
                    registration.kind,
                ),
            )
    return EffectRegistry(entries)


def _validate_cross_kind_ids(
    capabilities: CapabilityRegistry,
    schemas: SchemaRegistry,
    resources: ResourceRegistry,
    effects: EffectRegistry,
) -> None:
    kinds: tuple[tuple[str, Mapping[str, object]], ...] = (
        ("capability", capabilities.entries),
        ("schema", schemas.entries),
        ("resource", resources.entries),
        ("effect", effects.entries),
    )
    ownership: dict[str, str] = {}
    for kind, entries in kinds:
        for entry_id in entries:
            previous = ownership.get(entry_id)
            if previous is not None:
                raise RegistryConflict(f"cross-kind registry id: {entry_id} is both {previous} and {kind}")
            ownership[entry_id] = kind


def _validate_bindings(
    capabilities: CapabilityRegistry,
    resources: ResourceRegistry,
) -> None:
    for binding_id, binding in capabilities.bindings.items():
        for resource_id in binding.resource_ids:
            if resource_id not in resources.entries:
                raise RegistryConflict(f"unknown binding resource for {binding_id}: {resource_id}")


def _freeze_binding_data(value: object) -> object:
    try:
        return freeze_json(value)
    except (TypeError, ValueError) as error:
        raise RegistryConflict(f"invalid binding data: {error}") from error


def _owned_id(value: str, owner_id: str, kind: str) -> str:
    qualified = _qualified_id(value, f"{kind} id")
    if not qualified.startswith(f"{owner_id}."):
        raise RegistryConflict(f"{kind} id is not owned by {owner_id}: {qualified}")
    return qualified


def _qualified_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise RegistryConflict(f"invalid {kind}: {value!r}")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise RegistryConflict(f"invalid {kind}: {value!r}") from error


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if values.count(value) > 1}))


__all__ = ["RegistryConflict"]
