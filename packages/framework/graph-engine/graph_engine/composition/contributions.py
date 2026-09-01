from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import cast

from pydantic import (
    SerializerFunctionWrapHandler,
    StrictFloat,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.models import (
    AttemptContractRef,
    CapabilityBindingEntry,
    CommitValidatorEntry,
    ContributionAuthority,
    ExecutableKind,
    RegistrySet,
    ResourceEntry,
    SchemaEntry,
    SourceKey,
    SourceRole,
    TaskHandlerEntry,
)
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import FrozenJSONValue, freeze_json, thaw_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    CommitValidator,
    EffectPolicy,
    EffectRegistration,
    FrozenModel,
    PluginContribution,
    PluginDescriptor,
    ResourceContribution,
    SchemaContribution,
    TaskHandler,
    validate_contribution,
)


class ContributionValueError(GraphEngineError):
    """A provenance-free selected contribution is not closed."""


@dataclass(frozen=True, slots=True)
class ValidatedContribution:
    owner_id: str
    descriptor: PluginDescriptor
    contribution: PluginContribution
    task_handlers: tuple[tuple[str, TaskHandler], ...]
    commit_validators: tuple[tuple[str, CommitValidator], ...]
    schemas: tuple[SchemaEntry, ...]
    resources: tuple[ResourceEntry, ...]
    effects: tuple[EffectRegistration, ...]
    bindings: tuple[CapabilityBindingContribution, ...]


def validate_contribution_values(
    values: tuple[tuple[str, PluginDescriptor, PluginContribution], ...],
) -> tuple[ValidatedContribution, ...]:
    """Validate and normalize the six provenance-free contribution categories."""

    schemas: dict[str, SchemaEntry] = {}
    resources: dict[str, ResourceEntry] = {}
    tasks: dict[str, TaskHandler] = {}
    bindings: dict[str, CapabilityBindingContribution] = {}
    all_ids: dict[str, str] = {}
    validated: list[ValidatedContribution] = []

    def reserve(entry_id: str, kind: str) -> None:
        previous = all_ids.get(entry_id)
        if previous is not None:
            if previous == kind:
                raise ContributionValueError(f"duplicate {kind} id: {entry_id}")
            raise ContributionValueError(f"cross-kind registry id: {entry_id} is both {previous} and {kind}")
        all_ids[entry_id] = kind

    for owner_id, descriptor, contribution in values:
        _qualified_id(owner_id, "selected plugin id")
        if descriptor.plugin_id != owner_id:
            raise ContributionValueError(f"plugin descriptor owner disagrees: {owner_id}")
        try:
            validate_contribution(descriptor, contribution)
        except (TypeError, ValueError) as error:
            raise ContributionValueError(str(error)) from error

        selected_tasks: list[tuple[str, TaskHandler]] = []
        selected_validators: list[tuple[str, CommitValidator]] = []
        selected_schemas: list[SchemaEntry] = []
        selected_resources: list[ResourceEntry] = []
        for capability_id, handler in contribution.task_handlers.items():
            _owned_id(capability_id, owner_id, "task handler")
            reserve(capability_id, "task handler")
            if not callable(getattr(handler, "execute", None)):
                raise ContributionValueError(f"task handler has no execute method: {capability_id}")
            tasks[capability_id] = handler
            selected_tasks.append((capability_id, handler))
        for capability_id, validator in contribution.commit_validators.items():
            _owned_id(capability_id, owner_id, "commit validator")
            reserve(capability_id, "commit validator")
            if not callable(getattr(validator, "validate", None)):
                raise ContributionValueError(f"commit validator has no validate method: {capability_id}")
            selected_validators.append((capability_id, validator))
        for schema in contribution.schemas:
            if not isinstance(schema, SchemaContribution):
                raise ContributionValueError(f"plugin {owner_id} contributed an invalid schema entry")
            _owned_id(schema.schema_id, owner_id, "schema")
            reserve(schema.schema_id, "schema")
            try:
                entry = SchemaEntry.from_content(
                    schema_id=schema.schema_id,
                    owner_id=owner_id,
                    media_type=schema.media_type,
                    content=schema.content,
                )
            except (TypeError, ValueError) as error:
                raise ContributionValueError(f"invalid schema content: {schema.schema_id}") from error
            schemas[schema.schema_id] = entry
            selected_schemas.append(entry)
        for resource in contribution.resources:
            if not isinstance(resource, ResourceContribution):
                raise ContributionValueError(f"plugin {owner_id} contributed an invalid resource entry")
            _owned_id(resource.resource_id, owner_id, "resource")
            reserve(resource.resource_id, "resource")
            entry = ResourceEntry(
                resource_id=resource.resource_id,
                owner_id=owner_id,
                media_type=resource.media_type,
                content=resource.content,
                sha256=hashlib.sha256(resource.content).hexdigest(),
            )
            resources[resource.resource_id] = entry
            selected_resources.append(entry)
        for registration in contribution.effects:
            if not isinstance(registration, EffectRegistration):
                raise ContributionValueError(f"plugin {owner_id} contributed an invalid effect entry")
            _owned_id(registration.kind, owner_id, "effect")
            reserve(registration.kind, "effect")
            if not callable(getattr(registration.handler, "apply", None)) or not callable(
                getattr(registration.handler, "reconcile", None)
            ):
                raise ContributionValueError(
                    f"effect handler must provide apply and reconcile: {registration.kind}"
                )
            if not isinstance(registration.policy, EffectPolicy):
                raise ContributionValueError(f"effect policy is invalid: {registration.kind}")
        for binding in contribution.bindings:
            if not isinstance(binding, CapabilityBindingContribution):
                raise ContributionValueError(f"plugin {owner_id} contributed an invalid binding entry")
            _owned_id(binding.capability_id, owner_id, "binding")
            reserve(binding.capability_id, "binding")
            try:
                freeze_json(binding.data)
            except (TypeError, ValueError) as error:
                raise ContributionValueError(f"invalid binding data: {error}") from error
            if len(binding.resource_ids) != len(set(binding.resource_ids)):
                raise ContributionValueError(f"duplicate binding resource id: {binding.capability_id}")
            bindings[binding.capability_id] = binding
        validated.append(
            ValidatedContribution(
                owner_id=owner_id,
                descriptor=descriptor,
                contribution=contribution,
                task_handlers=tuple(selected_tasks),
                commit_validators=tuple(selected_validators),
                schemas=tuple(selected_schemas),
                resources=tuple(selected_resources),
                effects=tuple(contribution.effects),
                bindings=tuple(contribution.bindings),
            )
        )

    _reject_alias_cycles(bindings)
    for item in validated:
        for registration in item.effects:
            if registration.intent_schema_id not in schemas:
                raise ContributionValueError(
                    f"unknown effect intent schema for {registration.kind}: {registration.intent_schema_id}"
                )
            if registration.receipt_schema_id not in schemas:
                raise ContributionValueError(
                    f"unknown effect receipt schema for {registration.kind}: {registration.receipt_schema_id}"
                )
        for binding in item.bindings:
            if binding.target_capability_id not in tasks:
                raise ContributionValueError(
                    f"unknown target capability for binding {binding.capability_id}: "
                    f"{binding.target_capability_id}"
                )
            for resource_id in binding.resource_ids:
                if resource_id not in resources:
                    raise ContributionValueError(
                        f"unknown binding resource for {binding.capability_id}: {resource_id}"
                    )
    return tuple(validated)


class ContributionSourceKeyProjection(FrozenModel):
    role: SourceRole
    owner_id: str


class ExecutableContributionProjection(FrozenModel):
    capability_id: str
    implementation: FrozenJSONValue
    implementation_digest: str


class SchemaContributionProjection(FrozenModel):
    schema_id: str
    media_type: str
    content_base64: str
    content_sha256: str
    dialect: str | None

    @model_validator(mode="after")
    def _authenticate_content(self) -> SchemaContributionProjection:
        _authenticate_base64(self.content_base64, self.content_sha256, "schema")
        return self


class ResourceContributionProjection(FrozenModel):
    resource_id: str
    media_type: str
    content_base64: str
    content_sha256: str

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resource contribution media type must be non-empty text")
        return value

    @model_validator(mode="after")
    def _authenticate_content(self) -> ResourceContributionProjection:
        _authenticate_base64(self.content_base64, self.content_sha256, "resource")
        return self


class EffectPolicyProjection(FrozenModel):
    max_attempts: StrictInt
    timeout_seconds: StrictFloat
    backoff_seconds: StrictFloat


class EffectContributionProjection(FrozenModel):
    kind: str
    intent_schema_id: str
    receipt_schema_id: str
    policy: EffectPolicyProjection
    apply_implementation: FrozenJSONValue
    apply_implementation_digest: str
    reconcile_implementation: FrozenJSONValue
    reconcile_implementation_digest: str


class BindingContributionProjection(FrozenModel):
    capability_id: str
    target_capability_id: str
    data: FrozenJSONValue
    resource_ids: tuple[str, ...]
    secret_handles: tuple[str, ...] = ()
    contract_id: str | None = None

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError(f"invalid binding contract id: {value!r}") from error

    @model_serializer(mode="wrap")
    def _omit_none_contract_id(self, serializer: SerializerFunctionWrapHandler) -> object:
        data = serializer(self)
        if isinstance(data, dict) and data.get("contract_id") is None:
            data.pop("contract_id", None)
        return data


class ContributionProjection(FrozenModel):
    owner_id: str
    source_key: ContributionSourceKeyProjection
    source_digest: str
    task_handlers: tuple[ExecutableContributionProjection, ...]
    commit_validators: tuple[ExecutableContributionProjection, ...]
    schemas: tuple[SchemaContributionProjection, ...]
    resources: tuple[ResourceContributionProjection, ...]
    effects: tuple[EffectContributionProjection, ...]
    bindings: tuple[BindingContributionProjection, ...]
    attempt_contracts: tuple[AttemptContractRef, ...] = ()

    @field_validator(
        "task_handlers",
        "commit_validators",
        "schemas",
        "resources",
        "effects",
        "bindings",
        "attempt_contracts",
    )
    @classmethod
    def _validate_canonical_category(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        identifiers = tuple(_projection_identifier(value) for value in values)
        if identifiers != tuple(sorted(identifiers)) or len(identifiers) != len(set(identifiers)):
            raise ValueError("contribution category requires unique canonical order")
        return values

    @model_validator(mode="after")
    def _validate_closed_projection(self) -> ContributionProjection:
        _qualified_id(self.owner_id, "contribution owner id")
        if self.source_key.owner_id != self.owner_id or self.source_key.role not in {
            SourceRole.PLUGIN,
            SourceRole.CONFIG,
        }:
            raise ValueError("contribution source key disagrees with its owner")
        if self.source_key.role is SourceRole.CONFIG and self.attempt_contracts:
            raise ValueError("configuration-tree contributions cannot declare attempt contracts")
        _validate_projection_digest(self.source_digest, "contribution source")

        category_ids = (
            *((item.capability_id, "task handler") for item in self.task_handlers),
            *((item.capability_id, "commit validator") for item in self.commit_validators),
            *((item.schema_id, "schema") for item in self.schemas),
            *((item.resource_id, "resource") for item in self.resources),
            *((item.kind, "effect") for item in self.effects),
            *((item.capability_id, "binding") for item in self.bindings),
            *((item.contract_id, "attempt contract") for item in self.attempt_contracts),
        )
        seen: dict[str, str] = {}
        for entry_id, kind in category_ids:
            _owned_id(entry_id, self.owner_id, kind)
            previous = seen.get(entry_id)
            if previous is not None:
                raise ValueError(f"cross-kind contribution id: {entry_id} is both {previous} and {kind}")
            seen[entry_id] = kind

        for item in self.task_handlers:
            _validate_executable_projection(
                item.implementation,
                item.implementation_digest,
                ExecutableKind.TASK_HANDLER,
                item.capability_id,
                self,
            )
        for item in self.commit_validators:
            _validate_executable_projection(
                item.implementation,
                item.implementation_digest,
                ExecutableKind.COMMIT_VALIDATOR,
                item.capability_id,
                self,
            )

        for item in self.schemas:
            content = _authenticate_base64(item.content_base64, item.content_sha256, "schema")
            entry = SchemaEntry.from_content(
                schema_id=item.schema_id,
                owner_id=self.owner_id,
                media_type=item.media_type,
                content=content,
            )
            if entry.dialect != item.dialect:
                raise ValueError("schema contribution dialect does not authenticate its content")
        for item in self.effects:
            _qualified_id(item.intent_schema_id, "effect intent schema id")
            _qualified_id(item.receipt_schema_id, "effect receipt schema id")
            EffectPolicy.model_validate(item.policy.model_dump(mode="json"))
            _validate_executable_projection(
                item.apply_implementation,
                item.apply_implementation_digest,
                ExecutableKind.EFFECT_APPLY,
                item.kind,
                self,
            )
            _validate_executable_projection(
                item.reconcile_implementation,
                item.reconcile_implementation_digest,
                ExecutableKind.EFFECT_RECONCILE,
                item.kind,
                self,
            )
        for item in self.bindings:
            _qualified_id(item.target_capability_id, "binding target capability id")
            if item.contract_id is not None:
                _qualified_id(item.contract_id, "binding contract id")
            if len(item.resource_ids) != len(set(item.resource_ids)):
                raise ValueError(f"duplicate binding resource id: {item.capability_id}")
            for resource_id in item.resource_ids:
                _qualified_id(resource_id, "binding resource id")
        return self

    @classmethod
    def from_authority(cls, authority: ContributionAuthority) -> ContributionProjection:
        contribution = authority.contribution
        executable = {
            (item.provenance.kind, item.provenance.registry_id): item.provenance
            for item in authority.authorities
        }
        return cls(
            owner_id=authority.owner_id,
            source_key=ContributionSourceKeyProjection(
                role=authority.source_key.role,
                owner_id=authority.source_key.owner_id,
            ),
            source_digest=authority.source_digest,
            task_handlers=tuple(
                ExecutableContributionProjection(
                    capability_id=capability_id,
                    implementation=executable[(ExecutableKind.TASK_HANDLER, capability_id)].projection(),
                    implementation_digest=executable[(ExecutableKind.TASK_HANDLER, capability_id)].digest,
                )
                for capability_id in sorted(contribution.task_handlers)
            ),
            commit_validators=tuple(
                ExecutableContributionProjection(
                    capability_id=capability_id,
                    implementation=executable[(ExecutableKind.COMMIT_VALIDATOR, capability_id)].projection(),
                    implementation_digest=executable[(ExecutableKind.COMMIT_VALIDATOR, capability_id)].digest,
                )
                for capability_id in sorted(contribution.commit_validators)
            ),
            schemas=tuple(
                _schema_projection(authority.owner_id, item)
                for item in sorted(contribution.schemas, key=lambda item: item.schema_id)
            ),
            resources=tuple(
                _resource_projection(item)
                for item in sorted(contribution.resources, key=lambda item: item.resource_id)
            ),
            effects=tuple(
                EffectContributionProjection(
                    kind=item.kind,
                    intent_schema_id=item.intent_schema_id,
                    receipt_schema_id=item.receipt_schema_id,
                    policy=EffectPolicyProjection(
                        max_attempts=item.policy.max_attempts,
                        timeout_seconds=item.policy.timeout_seconds,
                        backoff_seconds=item.policy.backoff_seconds,
                    ),
                    apply_implementation=executable[(ExecutableKind.EFFECT_APPLY, item.kind)].projection(),
                    apply_implementation_digest=executable[(ExecutableKind.EFFECT_APPLY, item.kind)].digest,
                    reconcile_implementation=executable[
                        (ExecutableKind.EFFECT_RECONCILE, item.kind)
                    ].projection(),
                    reconcile_implementation_digest=executable[
                        (ExecutableKind.EFFECT_RECONCILE, item.kind)
                    ].digest,
                )
                for item in sorted(contribution.effects, key=lambda item: item.kind)
            ),
            bindings=tuple(
                BindingContributionProjection(
                    capability_id=item.capability_id,
                    target_capability_id=item.target_capability_id,
                    data=freeze_json(item.data),
                    resource_ids=tuple(item.resource_ids),
                    secret_handles=tuple(item.secret_handles),
                    contract_id=item.contract_id,
                )
                for item in sorted(contribution.bindings, key=lambda item: item.capability_id)
            ),
            attempt_contracts=tuple(authority.attempt_contracts),
        )

    @classmethod
    def from_registry_owner(
        cls,
        registries: RegistrySet,
        authority: ContributionAuthority,
    ) -> ContributionProjection:
        owner_id = authority.owner_id
        task_handlers: list[ExecutableContributionProjection] = []
        validators: list[ExecutableContributionProjection] = []
        bindings: list[BindingContributionProjection] = []
        for capability_id, entry in registries.capabilities.entries.items():
            if entry.owner_id != owner_id:
                continue
            if isinstance(entry, TaskHandlerEntry):
                _require_generation(entry.authority, authority)
                task_handlers.append(
                    ExecutableContributionProjection(
                        capability_id=capability_id,
                        implementation=entry.provenance.projection(),
                        implementation_digest=entry.provenance.digest,
                    )
                )
            elif isinstance(entry, CommitValidatorEntry):
                _require_generation(entry.authority, authority)
                validators.append(
                    ExecutableContributionProjection(
                        capability_id=capability_id,
                        implementation=entry.provenance.projection(),
                        implementation_digest=entry.provenance.digest,
                    )
                )
            elif isinstance(entry, CapabilityBindingEntry):
                bindings.append(
                    BindingContributionProjection(
                        capability_id=capability_id,
                        target_capability_id=entry.target_capability_id,
                        data=entry.data,
                        resource_ids=entry.resource_ids,
                        secret_handles=entry.secret_handles,
                        contract_id=entry.contract_id,
                    )
                )
        effects = []
        for kind, entry in registries.effects.entries.items():
            if entry.owner_id != owner_id:
                continue
            _require_generation(entry.authority, authority)
            effects.append(
                EffectContributionProjection(
                    kind=kind,
                    intent_schema_id=entry.intent_schema_id,
                    receipt_schema_id=entry.receipt_schema_id,
                    policy=EffectPolicyProjection(
                        max_attempts=entry.policy.max_attempts,
                        timeout_seconds=entry.policy.timeout_seconds,
                        backoff_seconds=entry.policy.backoff_seconds,
                    ),
                    apply_implementation=entry.apply_provenance.projection(),
                    apply_implementation_digest=entry.apply_provenance.digest,
                    reconcile_implementation=entry.reconcile_provenance.projection(),
                    reconcile_implementation_digest=entry.reconcile_provenance.digest,
                )
            )
        return cls(
            owner_id=owner_id,
            source_key=ContributionSourceKeyProjection(
                role=authority.source_key.role,
                owner_id=authority.source_key.owner_id,
            ),
            source_digest=authority.source_digest,
            task_handlers=tuple(task_handlers),
            commit_validators=tuple(validators),
            schemas=tuple(
                SchemaContributionProjection(
                    schema_id=schema_id,
                    media_type=entry.media_type,
                    content_base64=base64.b64encode(entry.content).decode("ascii"),
                    content_sha256=entry.sha256,
                    dialect=entry.dialect,
                )
                for schema_id, entry in registries.schemas.entries.items()
                if entry.owner_id == owner_id
            ),
            resources=tuple(
                ResourceContributionProjection(
                    resource_id=resource_id,
                    media_type=entry.media_type,
                    content_base64=base64.b64encode(entry.content).decode("ascii"),
                    content_sha256=entry.sha256,
                )
                for resource_id, entry in registries.resources.entries.items()
                if entry.owner_id == owner_id
            ),
            effects=tuple(effects),
            bindings=tuple(bindings),
            attempt_contracts=tuple(authority.attempt_contracts),
        )

    def validate_selected(
        self,
        descriptor: PluginDescriptor,
        source_key: SourceKey,
        source_digest: str,
    ) -> None:
        if (
            self.owner_id != descriptor.plugin_id
            or self.source_key.role is not source_key.role
            or self.source_key.owner_id != source_key.owner_id
            or self.source_digest != source_digest
        ):
            raise ValueError("contribution authority disagrees with its selected plugin source")
        actual = {
            "task_handlers": {item.capability_id for item in self.task_handlers},
            "commit_validators": {item.capability_id for item in self.commit_validators},
            "schemas": {item.schema_id for item in self.schemas},
            "resources": {item.resource_id for item in self.resources},
            "effects": {item.kind for item in self.effects},
            "bindings": {item.capability_id for item in self.bindings},
            "attempt_contracts": {(item.contract_id, item.digest) for item in self.attempt_contracts},
        }
        expected = {
            "task_handlers": set(descriptor.task_handlers),
            "commit_validators": set(descriptor.commit_validators),
            "schemas": set(descriptor.schemas),
            "resources": set(descriptor.resources),
            "effects": set(descriptor.effects),
            "bindings": set(descriptor.bindings),
            "attempt_contracts": {(item.contract_id, item.digest) for item in descriptor.attempt_contracts},
        }
        if actual != expected:
            raise ValueError("contribution authority categories disagree with descriptor declarations")

    @model_serializer(mode="wrap")
    def _omit_empty_attempt_contracts(self, serializer: SerializerFunctionWrapHandler) -> object:
        data = serializer(self)
        if isinstance(data, dict) and not data.get("attempt_contracts"):
            data.pop("attempt_contracts", None)
        return data

    def model_json_projection(self) -> JSONValue:
        return cast(JSONValue, self.model_dump(mode="json"))

    def capability_registry_projection(self) -> list[JSONValue]:
        values: list[dict[str, JSONValue]] = [
            *(
                {
                    "capability_id": item.capability_id,
                    "owner_id": self.owner_id,
                    "kind": "task_handler",
                    "implementation": cast(JSONValue, thaw_json(item.implementation)),
                    "implementation_digest": item.implementation_digest,
                }
                for item in self.task_handlers
            ),
            *(
                {
                    "capability_id": item.capability_id,
                    "owner_id": self.owner_id,
                    "kind": "commit_validator",
                    "implementation": cast(JSONValue, thaw_json(item.implementation)),
                    "implementation_digest": item.implementation_digest,
                }
                for item in self.commit_validators
            ),
            *(
                {
                    "capability_id": item.capability_id,
                    "owner_id": self.owner_id,
                    "kind": "binding",
                    "target_capability_id": item.target_capability_id,
                    "data": cast(JSONValue, thaw_json(item.data)),
                    "resource_ids": list(item.resource_ids),
                    "secret_handles": list(item.secret_handles),
                    **({"contract_id": item.contract_id} if item.contract_id is not None else {}),
                }
                for item in self.bindings
            ),
        ]
        ordered = sorted(values, key=lambda item: cast(str, item["capability_id"]))
        return cast(list[JSONValue], ordered)

    def schema_registry_projection(self) -> list[JSONValue]:
        return [
            {
                "schema_id": item.schema_id,
                "owner_id": self.owner_id,
                "media_type": item.media_type,
                "sha256": item.content_sha256,
                "dialect": item.dialect,
            }
            for item in self.schemas
        ]

    def resource_registry_projection(self) -> list[JSONValue]:
        return [
            {
                "resource_id": item.resource_id,
                "owner_id": self.owner_id,
                "media_type": item.media_type,
                "sha256": item.content_sha256,
            }
            for item in self.resources
        ]

    def effect_registry_projection(self) -> list[JSONValue]:
        return [
            {
                "kind": item.kind,
                "owner_id": self.owner_id,
                "intent_schema_id": item.intent_schema_id,
                "receipt_schema_id": item.receipt_schema_id,
                "policy": item.policy.model_dump(mode="json"),
                "apply_implementation": cast(
                    JSONValue,
                    thaw_json(item.apply_implementation),
                ),
                "apply_implementation_digest": item.apply_implementation_digest,
                "reconcile_implementation": cast(
                    JSONValue,
                    thaw_json(item.reconcile_implementation),
                ),
                "reconcile_implementation_digest": item.reconcile_implementation_digest,
            }
            for item in self.effects
        ]


def contribution_authority_projection(authority: ContributionAuthority) -> dict[str, JSONValue]:
    return cast(
        dict[str, JSONValue], ContributionProjection.from_authority(authority).model_dump(mode="json")
    )


def validate_registry_contribution_authorities(
    registries: RegistrySet,
    authorities: Mapping[str, ContributionAuthority],
    descriptors: tuple[PluginDescriptor, ...],
) -> None:
    descriptor_by_id = {descriptor.plugin_id: descriptor for descriptor in descriptors}
    if len(descriptor_by_id) != len(descriptors) or set(authorities) != set(descriptor_by_id):
        raise ValueError("selected plugin contribution authorities disagree with descriptors")
    projections: list[ContributionProjection] = []
    for owner_id, authority in authorities.items():
        if not isinstance(authority, ContributionAuthority):
            raise TypeError("composition contribution authorities must be typed")
        descriptor = descriptor_by_id[owner_id]
        if authority.owner_id != owner_id or authority.descriptor != descriptor:
            raise ValueError(f"contribution authority disagrees with descriptor: {owner_id}")
        source = registries.sources.entries.get(authority.source_key)
        if source is None:
            raise ValueError(f"contribution authority has no selected source: {owner_id}")
        expected = ContributionProjection.from_authority(authority)
        expected.validate_selected(descriptor, authority.source_key, source.snapshot.digest)
        projections.append(expected)
        if expected != ContributionProjection.from_registry_owner(registries, authority):
            raise ValueError(f"registry values disagree with contribution authority: {owner_id}")
    validate_contribution_projection_set(tuple(projections))


def validate_contribution_projection_set(
    projections: tuple[ContributionProjection, ...],
) -> None:
    """Validate cross-plugin closure for canonical contribution projections."""

    owners = tuple(projection.owner_id for projection in projections)
    if len(owners) != len(set(owners)):
        raise ValueError("contribution projection owners must be unique")
    task_ids = {item.capability_id for projection in projections for item in projection.task_handlers}
    schema_ids = {item.schema_id for projection in projections for item in projection.schemas}
    resource_ids = {item.resource_id for projection in projections for item in projection.resources}
    all_ids: dict[str, str] = {}
    for projection in projections:
        categories = (
            *((item.capability_id, "task handler") for item in projection.task_handlers),
            *((item.capability_id, "commit validator") for item in projection.commit_validators),
            *((item.schema_id, "schema") for item in projection.schemas),
            *((item.resource_id, "resource") for item in projection.resources),
            *((item.kind, "effect") for item in projection.effects),
            *((item.capability_id, "binding") for item in projection.bindings),
            *((item.contract_id, "attempt contract") for item in projection.attempt_contracts),
        )
        for entry_id, kind in categories:
            previous = all_ids.get(entry_id)
            if previous is not None:
                raise ValueError(f"cross-kind contribution id: {entry_id} is both {previous} and {kind}")
            all_ids[entry_id] = kind
        for effect in projection.effects:
            if effect.intent_schema_id not in schema_ids:
                raise ValueError(f"unknown effect intent schema for {effect.kind}: {effect.intent_schema_id}")
            if effect.receipt_schema_id not in schema_ids:
                raise ValueError(
                    f"unknown effect receipt schema for {effect.kind}: {effect.receipt_schema_id}"
                )
        for binding in projection.bindings:
            if binding.target_capability_id not in task_ids:
                raise ValueError(
                    f"unknown target capability for binding {binding.capability_id}: "
                    f"{binding.target_capability_id}"
                )
            for resource_id in binding.resource_ids:
                if resource_id not in resource_ids:
                    raise ValueError(f"unknown binding resource for {binding.capability_id}: {resource_id}")


def _schema_projection(owner_id: str, item: SchemaContribution) -> SchemaContributionProjection:
    entry = SchemaEntry.from_content(
        schema_id=item.schema_id,
        owner_id=owner_id,
        media_type=item.media_type,
        content=item.content,
    )
    return SchemaContributionProjection(
        schema_id=item.schema_id,
        media_type=item.media_type,
        content_base64=base64.b64encode(item.content).decode("ascii"),
        content_sha256=entry.sha256,
        dialect=entry.dialect,
    )


def _resource_projection(item: ResourceContribution) -> ResourceContributionProjection:
    return ResourceContributionProjection(
        resource_id=item.resource_id,
        media_type=item.media_type,
        content_base64=base64.b64encode(item.content).decode("ascii"),
        content_sha256=hashlib.sha256(item.content).hexdigest(),
    )


def _authenticate_base64(encoded: str, digest: str, kind: str) -> bytes:
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError(f"{kind} contribution content is not canonical base64") from error
    if base64.b64encode(content).decode("ascii") != encoded:
        raise ValueError(f"{kind} contribution content is not canonical base64")
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError(f"{kind} contribution digest does not authenticate content")
    return content


def _validate_executable_projection(
    frozen: FrozenJSONValue,
    implementation_digest: str,
    kind: ExecutableKind,
    registry_id: str,
    contribution: ContributionProjection,
) -> None:
    projection = thaw_json(frozen)
    if not isinstance(projection, dict):
        raise ValueError("executable contribution provenance must be a mapping")
    embedded_digest = projection.get("digest")
    if embedded_digest != implementation_digest:
        raise ValueError("executable contribution digest disagrees with embedded provenance")
    _validate_projection_digest(implementation_digest, "executable implementation")
    unsigned = {key: value for key, value in projection.items() if key != "digest"}
    if canonical_digest(cast(JSONValue, unsigned)) != implementation_digest:
        raise ValueError("executable contribution digest does not authenticate provenance")
    if (
        projection.get("kind") != kind.value
        or projection.get("registry_id") != registry_id
        or projection.get("owner_id") != contribution.owner_id
        or projection.get("source_digest") != contribution.source_digest
        or projection.get("source_key")
        != {
            "role": contribution.source_key.role.value,
            "owner_id": contribution.source_key.owner_id,
        }
    ):
        raise ValueError("executable contribution provenance disagrees with its category")


def _validate_projection_digest(value: str, kind: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{kind} digest must be lowercase sha256")


def _projection_identifier(value: object) -> str:
    for name in ("capability_id", "schema_id", "resource_id", "kind", "contract_id"):
        identifier = getattr(value, name, None)
        if isinstance(identifier, str):
            return identifier
    raise TypeError("unsupported contribution projection entry")


def _require_generation(actual: ContributionAuthority, expected: ContributionAuthority) -> None:
    if actual is not expected:
        raise ValueError("registry executable mixes contribution authority generations")


def _reject_alias_cycles(bindings: Mapping[str, CapabilityBindingContribution]) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(alias_id: str) -> None:
        marker = state.get(alias_id, 0)
        if marker == 2:
            return
        if marker == 1:
            start = stack.index(alias_id)
            cycle = (*stack[start:], alias_id)
            raise ContributionValueError(f"alias cycle: {' -> '.join(cycle)}")
        state[alias_id] = 1
        stack.append(alias_id)
        target_id = bindings[alias_id].target_capability_id
        if target_id in bindings:
            visit(target_id)
        stack.pop()
        state[alias_id] = 2

    for alias_id in sorted(bindings):
        visit(alias_id)


def _owned_id(value: str, owner_id: str, kind: str) -> str:
    qualified = _qualified_id(value, f"{kind} id")
    if not qualified.startswith(f"{owner_id}."):
        raise ContributionValueError(f"{kind} id is not owned by {owner_id}: {qualified}")
    return qualified


def _qualified_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise ContributionValueError(f"invalid {kind}: {value!r}")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ContributionValueError(f"invalid {kind}: {value!r}") from error


__all__ = [
    "ContributionProjection",
    "ContributionValueError",
    "ValidatedContribution",
    "contribution_authority_projection",
    "validate_contribution_values",
    "validate_contribution_projection_set",
    "validate_registry_contribution_authorities",
]
