from __future__ import annotations

from dataclasses import replace
from itertools import permutations

import pytest

from graph_engine.composition.contributions import (
    ContributionProjection,
    validate_contribution_projection_set,
)
from graph_engine.composition.models import SourceKind
from graph_engine.composition.registries import RegistryConflict
from graph_engine.plugin_api import AttemptContractRef, CapabilityBindingContribution, PluginContribution

from .test_registries import _Handler, _authenticated, _source, build_registries

_CONTRACT = "warehouse.inventory.reserve.v1"
_OLD_CONTRACT = "assurance.intake.agent.intake.v1"
_TARGET = "toy.runtime.execute"


def _parts(
    contract_id: str = _CONTRACT,
    *,
    declared: bool = True,
    linked: bool = True,
    binding_id: str | None = None,
):
    owner = contract_id.rsplit(".", 2)[0]
    sources = tuple(
        _source(name, kind=SourceKind.WHEEL_PLUGIN) for name in (owner, "toy.runtime", "deployment.bindings")
    )
    contributions = (
        PluginContribution(
            attempt_contracts=(AttemptContractRef(contract_id=contract_id, digest="a" * 64),)
            if declared
            else (),
        ),
        PluginContribution(task_handlers={_TARGET: _Handler()}),
        PluginContribution(
            bindings=(
                CapabilityBindingContribution(
                    capability_id=contract_id if binding_id is None else binding_id,
                    contract_id=contract_id if linked else None,
                    target_capability_id=_TARGET,
                ),
            )
        ),
    )
    return sources, contributions, (owner, "toy.runtime", "deployment.bindings")


def _projections(parts):
    sources, contributions, _ = parts
    return tuple(
        ContributionProjection.from_authority(_authenticated(source, contribution).authority)
        for source, contribution in zip(sources, contributions, strict=True)
    )


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
def test_declared_contract_binding_is_product_neutral_and_order_independent(order) -> None:
    sources, contributions, owners = _parts()
    registry = build_registries(
        tuple(sources[i] for i in order),
        tuple(contributions[i] for i in order),
        tuple(owners[i] for i in order),
    )
    binding = registry.capabilities.bindings[_CONTRACT]
    assert binding.contract_id == _CONTRACT
    assert binding.owner_id == "deployment.bindings"
    assert binding.target_capability_id == _TARGET


@pytest.mark.parametrize("contract_id", (_CONTRACT, _OLD_CONTRACT))
@pytest.mark.parametrize("declared,linked", ((False, True), (True, False)))
def test_foreign_binding_requires_explicit_link_to_selected_contract(contract_id, declared, linked) -> None:
    with pytest.raises((RegistryConflict, ValueError), match="binding|contract|owner"):
        build_registries(*_parts(contract_id, declared=declared, linked=linked))


def test_owned_alias_can_link_a_declared_contract() -> None:
    registry = build_registries(*_parts(binding_id="deployment.bindings.reserve"))
    assert registry.capabilities.bindings["deployment.bindings.reserve"].contract_id == _CONTRACT


def test_owned_alias_cannot_reference_an_undeclared_contract() -> None:
    with pytest.raises((RegistryConflict, ValueError), match="contract"):
        build_registries(*_parts(declared=False, binding_id="deployment.bindings.reserve"))


def test_persisted_projection_accepts_generic_contract_bindings() -> None:
    projections = _projections(_parts())
    restored = tuple(
        ContributionProjection.model_validate_json(item.model_dump_json()) for item in projections
    )
    validate_contribution_projection_set(restored)


@pytest.mark.parametrize("order", tuple(permutations(range(4))))
def test_persisted_projection_rejects_duplicate_binding_after_contract_overlap(order) -> None:
    projections = _projections(_parts(_OLD_CONTRACT))
    binding = projections[-1].model_dump(mode="python")
    binding["owner_id"] = "deployment.other"
    binding["source_key"] = {"role": "plugin", "owner_id": "deployment.other"}
    duplicate = ContributionProjection.model_validate(binding)
    projections = (*projections, duplicate)
    with pytest.raises(ValueError, match="cross-kind|duplicate"):
        validate_contribution_projection_set(tuple(projections[i] for i in order))


def test_persisted_projection_rejects_missing_contract_declaration() -> None:
    projections = _projections(_parts(_OLD_CONTRACT))
    with pytest.raises(ValueError, match="contract"):
        validate_contribution_projection_set(projections[1:])


def test_binding_entry_cannot_replace_its_explicit_foreign_contract_link() -> None:
    registry = build_registries(*_parts())
    binding = registry.capabilities.bindings[_CONTRACT]
    with pytest.raises(ValueError, match="binding|owner"):
        replace(binding, contract_id=None)
