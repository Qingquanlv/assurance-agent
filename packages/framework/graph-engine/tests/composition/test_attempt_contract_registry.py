from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import BaseModel

from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import ProductLock, RegistryDigests, RegistryProjections
from graph_engine.composition.models import AttemptContractRegistry, SourceRole
from graph_engine.composition.registries import RegistryConflictError, build_attempt_registry
from graph_engine.plugin_api import (
    PluginContribution,
    PluginContractError,
    PluginDescriptor,
    ResourceClaims,
    realize_plugin,
)

from .test_lock_model import _lock, _product_lock


class IntakeInput(BaseModel):
    change_id: str


class IntakeOutput(BaseModel):
    status: str


class GenerationInput(BaseModel):
    prompt: str


class GenerationOutput(BaseModel):
    text: str


class AlteredIntakeInput(BaseModel):
    change_id: str
    extra: str = ""


def _descriptor(
    *,
    plugin_id: str = "assurance.intake",
    contract_id: str = "assurance.intake.prepare.v1",
    contract_digest: str,
    extra_contracts: tuple[tuple[str, str], ...] = (),
) -> PluginDescriptor:
    contracts = ((contract_id, contract_digest), *extra_contracts)
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api="2.0",
        task_handlers=(),
        commit_validators=(),
        attempt_contracts=tuple(
            {"contract_id": contract_id, "digest": digest} for contract_id, digest in contracts
        ),
    )


def _contribution(
    *items: tuple[str, str],
) -> PluginContribution:
    return PluginContribution(
        attempt_contracts=tuple(
            {"contract_id": contract_id, "digest": digest} for contract_id, digest in items
        )
    )


def descriptor_with(*, contract_digest: str) -> PluginDescriptor:
    return _descriptor(contract_digest=contract_digest)


def contribution_with(contract_digest: str) -> PluginContribution:
    return _contribution(("assurance.intake.prepare.v1", contract_digest))


def _contract(
    *,
    contract_id: str = "assurance.intake.prepare.v1",
    owner_id: str = "assurance.intake",
    handler_id: str = "assurance.intake.prepare",
    input_model: type[BaseModel] = IntakeInput,
    output_model: type[BaseModel] = IntakeOutput,
    resources: ResourceClaims | None = None,
    timeout_seconds: float = 30,
    validators: tuple[str, ...] = (),
) -> TaskAttemptContract[BaseModel, BaseModel]:
    return TaskAttemptContract(
        contract_id=contract_id,
        owner_id=owner_id,
        handler_id=handler_id,
        input_model=input_model,
        output_model=output_model,
        resources=ResourceClaims() if resources is None else resources,
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=timeout_seconds),
        validators=validators,
    )


def _record(
    contract: TaskAttemptContract[BaseModel, BaseModel],
    *,
    dependencies: tuple[str, ...] = (),
    available_handlers: MappingProxyType[str, str] | dict[str, str] | None = None,
    available_validators: MappingProxyType[str, str] | dict[str, str] | None = None,
    source_role: SourceRole = SourceRole.PLUGIN,
    handler: object | None = None,
) -> object:
    from graph_engine.composition.models import AttemptContractClaim

    handlers = (
        available_handlers if available_handlers is not None else {contract.handler_id: contract.owner_id}
    )
    validators = (
        available_validators
        if available_validators is not None
        else {validator_id: contract.owner_id for validator_id in contract.validators}
    )
    return AttemptContractClaim(
        contract=contract,
        dependencies=dependencies,
        available_handlers=MappingProxyType(dict(handlers)),
        available_validators=MappingProxyType(dict(validators)),
        source_role=source_role,
        handler=handler,
    )


def _intake_using_generation() -> object:
    contract = _contract(handler_id="assurance.generation.draft")
    return _record(
        contract,
        available_handlers={"assurance.generation.draft": "assurance.generation"},
    )


intake_contract_using_generation_handler = _intake_using_generation()


def test_realized_attempt_contracts_must_match_descriptor_digests() -> None:
    with pytest.raises(PluginContractError, match="attempt contract"):
        realize_plugin(descriptor_with(contract_digest="a" * 64), contribution_with("b" * 64))


def test_contract_cannot_bind_foreign_handler_without_dependency_authority() -> None:
    with pytest.raises(RegistryConflictError, match="owner"):
        build_attempt_registry([intake_contract_using_generation_handler])


def test_duplicate_attempt_contract_ids_are_rejected() -> None:
    first = _record(_contract())
    second = _record(_contract())
    with pytest.raises(RegistryConflictError, match="duplicate"):
        build_attempt_registry([first, second])


def test_unsorted_validators_are_rejected() -> None:
    contract = _contract(
        validators=("assurance.intake.validator.z.v1", "assurance.intake.validator.a.v1"),
    )
    with pytest.raises(RegistryConflictError, match="validator"):
        build_attempt_registry(
            [
                _record(
                    contract,
                    available_validators={
                        "assurance.intake.validator.z.v1": "assurance.intake",
                        "assurance.intake.validator.a.v1": "assurance.intake",
                    },
                )
            ]
        )


def test_duplicate_validators_are_rejected() -> None:
    contract = _contract(
        validators=("assurance.intake.validator.a.v1", "assurance.intake.validator.a.v1"),
    )
    with pytest.raises((RegistryConflictError, TypeError, ValueError), match="validator"):
        build_attempt_registry(
            [
                _record(
                    contract,
                    available_validators={"assurance.intake.validator.a.v1": "assurance.intake"},
                )
            ]
        )


def test_missing_handler_is_rejected() -> None:
    with pytest.raises(RegistryConflictError, match="handler"):
        build_attempt_registry([_record(_contract(), available_handlers={})])


def test_missing_validator_is_rejected() -> None:
    contract = _contract(validators=("assurance.intake.validator.missing.v1",))
    with pytest.raises(RegistryConflictError, match="validator"):
        build_attempt_registry([_record(contract, available_validators={})])


def test_missing_schema_model_identity_is_rejected() -> None:
    contract = _contract()
    object.__setattr__(contract, "input_model", object)
    with pytest.raises(RegistryConflictError, match="model|schema"):
        build_attempt_registry([_record(contract)])


def test_configuration_tree_contracts_are_rejected() -> None:
    with pytest.raises(RegistryConflictError, match="configuration|config"):
        build_attempt_registry([_record(_contract(), source_role=SourceRole.CONFIG)])


def test_extra_realized_contracts_are_rejected() -> None:
    descriptor = descriptor_with(contract_digest="a" * 64)
    extra = _contribution(
        ("assurance.intake.extra.v1", "c" * 64),
        ("assurance.intake.prepare.v1", "a" * 64),
    )
    with pytest.raises(PluginContractError, match="attempt contract"):
        realize_plugin(descriptor, extra)


def test_matching_descriptor_and_realized_attempt_contracts_realize() -> None:
    digest = "a" * 64
    realized = realize_plugin(descriptor_with(contract_digest=digest), contribution_with(digest))
    assert tuple(item.digest for item in realized.attempt_contracts) == (digest,)


def test_owned_handler_with_empty_validators_registers() -> None:
    contract = _contract(validators=())
    registry = build_attempt_registry([_record(contract, handler=object())])
    assert isinstance(registry, AttemptContractRegistry)
    assert tuple(registry.entries) == ("assurance.intake.prepare.v1",)
    entry = registry.entries["assurance.intake.prepare.v1"]
    assert entry.digest == canonical_digest(contract.canonical_projection())
    assert entry.validators == ()


def test_foreign_handler_is_allowed_with_dependency_authority() -> None:
    contract = _contract(handler_id="assurance.generation.draft")
    registry = build_attempt_registry(
        [
            _record(
                contract,
                dependencies=("assurance.generation",),
                available_handlers={"assurance.generation.draft": "assurance.generation"},
            )
        ]
    )
    assert "assurance.intake.prepare.v1" in registry.entries


def test_attempt_registry_digest_changes_with_contract_data() -> None:
    base = _contract(timeout_seconds=30)
    changed_timeout = _contract(timeout_seconds=45)
    changed_resources = _contract(resources=ResourceClaims(reads=("workspace/input",)))
    changed_validators = _contract(validators=("assurance.intake.validator.evidence.v1",))
    changed_schema = _contract(input_model=AlteredIntakeInput)
    base_registry = build_attempt_registry([_record(base)])
    assert base_registry.digest != build_attempt_registry([_record(changed_timeout)]).digest
    assert base_registry.digest != build_attempt_registry([_record(changed_resources)]).digest
    assert (
        base_registry.digest
        != build_attempt_registry(
            [
                _record(
                    changed_validators,
                    available_validators={"assurance.intake.validator.evidence.v1": "assurance.intake"},
                )
            ]
        ).digest
    )
    assert base_registry.digest != build_attempt_registry([_record(changed_schema)]).digest


def test_swapping_callables_does_not_change_data_projection() -> None:
    contract = _contract()
    first = build_attempt_registry([_record(contract, handler=object())])
    second = build_attempt_registry([_record(contract, handler=object())])
    assert first.digest == second.digest
    assert first.projection() == second.projection()


def test_product_lock_v3_digest_changes_with_attempt_registry() -> None:
    base = _contract(timeout_seconds=30)
    changed = _contract(timeout_seconds=45)
    base_lock = _product_lock_with_attempt_registry(build_attempt_registry([_record(base)]))
    changed_lock = _product_lock_with_attempt_registry(build_attempt_registry([_record(changed)]))
    assert base_lock.schema_version == "3"
    assert base_lock.digest != changed_lock.digest
    assert base_lock.registry_digests.attempt_contracts != changed_lock.registry_digests.attempt_contracts


def test_product_lock_includes_empty_attempt_contract_registry() -> None:
    lock = _lock()
    assert lock.schema_version == "3"
    assert b'"attempt_contracts":[]' in lock.canonical_bytes


def _product_lock_with_attempt_registry(registry: AttemptContractRegistry) -> ProductLock:
    lock = _product_lock()
    projections = RegistryProjections(
        sources=lock.registry_projections.sources,
        capabilities=lock.registry_projections.capabilities,
        schemas=lock.registry_projections.schemas,
        resources=lock.registry_projections.resources,
        effects=lock.registry_projections.effects,
        attempt_contracts=registry.projection(),
    )
    digests = RegistryDigests(
        sources=lock.registry_digests.sources,
        capabilities=lock.registry_digests.capabilities,
        schemas=lock.registry_digests.schemas,
        resources=lock.registry_digests.resources,
        effects=lock.registry_digests.effects,
        attempt_contracts=registry.digest,
    )
    return ProductLock.create(
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=projections,
        registry_digests=digests,
        configuration=lock.configuration,
        configuration_digest=lock.configuration_digest,
        capability_bindings=lock.capability_bindings,
        capability_bindings_digest=lock.capability_bindings_digest,
    )
