from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from graph_engine.composition.contributions import (
    ContributionValueError,
    ValidatedContribution,
    validate_contribution_values,
    validate_registry_contribution_authorities,
)
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
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    CommitValidator,
    PluginContribution,
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
    values: ValidatedContribution


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
    effect_view = _build_effect_registry(owned)
    _validate_executable_registry_sets(owned, capability_view, effect_view)
    registries = RegistrySet(
        sources=source_view,
        capabilities=capability_view,
        schemas=schema_view,
        resources=resource_view,
        effects=effect_view,
    )
    validate_registry_contribution_authorities(
        registries,
        {item.owner_id: item.authenticated.authority for item in owned},
        tuple(item.authenticated.descriptor for item in owned),
    )
    return registries


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
    paired: list[tuple[str, SourceEntry, AuthenticatedContribution]] = []
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
        paired.append((owner_id, source, authenticated))
    try:
        values = validate_contribution_values(
            tuple(
                (owner_id, authenticated.descriptor, authenticated.contribution)
                for owner_id, _, authenticated in paired
            )
        )
    except ContributionValueError as error:
        raise RegistryConflict(str(error)) from error
    return tuple(
        _OwnedContribution(
            owner_id,
            source,
            authenticated.contribution,
            authenticated,
            validated,
        )
        for (owner_id, source, authenticated), validated in zip(paired, values, strict=True)
    )


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
        entries.update((schema.schema_id, schema) for schema in owned.values.schemas)
    return SchemaRegistry(entries)


def _build_resource_registry(contributions: tuple[_OwnedContribution, ...]) -> ResourceRegistry:
    entries: dict[str, ResourceEntry] = {}
    for owned in contributions:
        entries.update((resource.resource_id, resource) for resource in owned.values.resources)
    return ResourceRegistry(entries)


def _build_capability_registry(
    contributions: tuple[_OwnedContribution, ...],
) -> CapabilityRegistry:
    entries: dict[str, TaskHandlerEntry | CommitValidatorEntry | CapabilityBindingEntry] = {}
    task_handlers: dict[str, TaskHandler] = {}
    task_handler_entries: dict[str, TaskHandlerEntry] = {}
    validators: dict[str, CommitValidator] = {}
    binding_contributions: dict[str, tuple[str, CapabilityBindingContribution]] = {}

    for owned in contributions:
        for capability_id, handler in owned.values.task_handlers:
            entry = TaskHandlerEntry(
                capability_id,
                owned.owner_id,
                handler,
                owned.authenticated.executable(ExecutableKind.TASK_HANDLER, capability_id),
                owned.authenticated.authority,
            )
            entries[capability_id] = entry
            task_handlers[capability_id] = handler
            task_handler_entries[capability_id] = entry
        for capability_id, validator in owned.values.commit_validators:
            entry = CommitValidatorEntry(
                capability_id,
                owned.owner_id,
                validator,
                owned.authenticated.executable(ExecutableKind.COMMIT_VALIDATOR, capability_id),
                owned.authenticated.authority,
            )
            entries[capability_id] = entry
            validators[capability_id] = validator
        for binding in owned.values.bindings:
            binding_contributions[binding.capability_id] = (owned.owner_id, binding)

    bindings: dict[str, CapabilityBindingEntry] = {}
    for capability_id in sorted(binding_contributions):
        owner_id, binding = binding_contributions[capability_id]
        target = task_handlers.get(binding.target_capability_id)
        target_entry = task_handler_entries.get(binding.target_capability_id)
        if target is None or target_entry is None:  # pragma: no cover - validated closed set
            raise RegistryConflict("validated binding target disappeared")
        entry = CapabilityBindingEntry._from_target(
            capability_id=capability_id,
            owner_id=owner_id,
            target_capability_id=binding.target_capability_id,
            data=binding.data,
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


def _build_effect_registry(contributions: tuple[_OwnedContribution, ...]) -> EffectRegistry:
    entries: dict[str, EffectEntry] = {}
    for owned in contributions:
        for registration in owned.values.effects:
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
                authority=owned.authenticated.authority,
            )
    return EffectRegistry(entries)


def _validate_executable_registry_sets(
    contributions: tuple[_OwnedContribution, ...],
    capabilities: CapabilityRegistry,
    effects: EffectRegistry,
) -> None:
    expected = {
        (owned.owner_id, kind, registry_id)
        for owned in contributions
        for kind, registry_id in owned.authenticated.authority.keys
    }
    actual: set[tuple[str, ExecutableKind, str]] = set()
    for entry in capabilities.entries.values():
        if isinstance(entry, TaskHandlerEntry):
            actual.add((entry.owner_id, ExecutableKind.TASK_HANDLER, entry.capability_id))
        elif isinstance(entry, CommitValidatorEntry):
            actual.add((entry.owner_id, ExecutableKind.COMMIT_VALIDATOR, entry.capability_id))
    for entry in effects.entries.values():
        actual.add((entry.owner_id, ExecutableKind.EFFECT_APPLY, entry.kind))
        actual.add((entry.owner_id, ExecutableKind.EFFECT_RECONCILE, entry.kind))
    if actual != expected:
        raise RegistryConflict("executable registry entries disagree with declared executable set")


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
