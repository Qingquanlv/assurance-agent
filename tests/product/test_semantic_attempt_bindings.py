from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")

_PURE_FUNCTION_IDS = (
    "assurance.generation.complete",
    "assurance.generation.review-round.advance",
    "assurance.healing.repair-round.advance",
    "assurance.intake.review-round.advance",
)


def test_product_has_exactly_one_runtime_binding_per_agent_contract() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS

    contracts = all_feature_agent_contracts()
    assert len(contracts) == 33
    assert set(AGENT_RUNTIME_BINDINGS) == set(contracts)
    assert len(AGENT_RUNTIME_BINDINGS) == 33


def test_semantic_bindings_coexist_with_legacy_aliases_during_shadow(installed_sources) -> None:
    from assurance_product.agent_contracts import LEGACY_AGENT_PHASE_ALIASES
    from assurance_product.product import resolve_assurance_composition

    request = request_for("opencode", installed_sources)
    composition = resolve_assurance_composition(request)
    assert len(composition.semantic_attempt_contracts) == 41
    assert len(LEGACY_AGENT_PHASE_ALIASES) == 99


@pytest.mark.parametrize(
    "extra",
    [
        {"validators": ()},
        {"resources": {"reads": ("qa",)}},
        {"output_model": "CaseDesignOutput"},
    ],
)
def test_product_runtime_binding_rejects_feature_authority(extra: dict[str, object]) -> None:
    from agent_runtime_contracts import AgentRuntimeBinding

    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS

    payload = next(iter(AGENT_RUNTIME_BINDINGS.values())).model_dump()
    with pytest.raises(ValidationError, match="extra"):
        AgentRuntimeBinding.model_validate({**payload, **extra})


def test_boot_rejects_runtime_handler_outside_product_closure(installed_sources) -> None:
    from agent_runtime_contracts import AgentRuntimeBinding

    from assurance_product.product import AssuranceCompositionError, resolve_assurance_composition
    from assurance_product.runtime_bindings import (
        AGENT_RUNTIME_BINDINGS,
        boot_semantic_attempt_contracts,
    )

    request = request_for("opencode", installed_sources)
    composition = resolve_assurance_composition(request)
    catalog = next(iter(AGENT_RUNTIME_BINDINGS.values()))
    foreign = AgentRuntimeBinding.model_validate(
        {
            **catalog.model_dump(),
            "runtime_handler_id": "runtime.unauthenticated.execute",
        }
    )
    with pytest.raises(AssuranceCompositionError, match="dependency closure"):
        boot_semantic_attempt_contracts(composition, {foreign.contract_id: foreign})


def test_semantic_registry_omits_pure_functions_and_keeps_validators_unbound(
    installed_sources,
) -> None:
    from graph_engine.attempts import ResolvedAttemptContract

    from assurance_product.product import resolve_assurance_composition

    request = request_for("opencode", installed_sources)
    composition = resolve_assurance_composition(request)
    resolved = composition.semantic_attempt_contracts
    assert len(resolved) == 41
    assert all(isinstance(item, ResolvedAttemptContract) for item in resolved.values())
    assert all(item.contract.validators == () for item in resolved.values())
    assert all(pure_id not in resolved for pure_id in _PURE_FUNCTION_IDS)
