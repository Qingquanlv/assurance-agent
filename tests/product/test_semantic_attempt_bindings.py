from __future__ import annotations

import inspect

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


def test_semantic_bindings_are_the_only_live_agent_ids(installed_sources) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.product import resolve_assurance_composition

    request = request_for("opencode", installed_sources)
    composition = resolve_assurance_composition(request)
    assert len(composition.semantic_attempt_contracts) == 41
    assert len(AGENT_EXECUTION_CONTRACTS) == 33
    assert not any(item.startswith("assurance.product.agent.") for item in AGENT_EXECUTION_CONTRACTS)


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


def test_raw_runtime_rows_are_canonical_and_digest_locked() -> None:
    from agent_runtime_contracts import RawAgentRuntimeBindingProjectionV1
    from graph_engine.canonical import canonical_digest

    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import RAW_AGENT_RUNTIME_BINDING_ROWS

    contracts = all_feature_agent_contracts()
    rows = RAW_AGENT_RUNTIME_BINDING_ROWS
    assert len(rows) == 33
    assert tuple(row.contract_id for row in rows) == tuple(sorted(contracts))
    assert all(isinstance(row, RawAgentRuntimeBindingProjectionV1) for row in rows)
    assert all(row.schema_version == "raw-agent-runtime-binding-v1" for row in rows)
    assert all(row.activity_recovery == "adopt-observe-reconcile-v1" for row in rows)
    for row in rows:
        contract = contracts[row.contract_id]
        assert row.contract_digest == canonical_digest(contract.canonical_projection())  # type: ignore[arg-type]
        assert row.adapter == row.provider
        assert row.runtime_handler_id == f"runtime.{row.adapter}.execute"


def test_raw_bindings_do_not_resolve_through_legacy_aliases() -> None:
    from assurance_product import runtime_bindings

    source = inspect.getsource(runtime_bindings.runtime_bindings_from_composition)
    assert "alias_ids_for_prepare" not in source
    assert "LEGACY_AGENT_PHASE_ALIASES" not in source
    assert "bindings[" not in source


def test_boot_uses_resolved_raw_executor_for_every_agent_occurrence(installed_sources) -> None:
    from agent_runtime_contracts import ResolvedRawAgentExecutor

    from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts
    from assurance_product.product import resolve_assurance_composition

    request = request_for("opencode", installed_sources)
    composition = resolve_assurance_composition(request)
    agents = all_feature_agent_contracts()
    tasks = all_feature_task_contracts()
    resolved = composition.semantic_attempt_contracts
    assert len(agents) == 33
    assert len(tasks) == 8
    task_ids = {contract.contract_id for contract in tasks.values()}
    assert set(agents) | task_ids == set(resolved)
    for contract_id in agents:
        assert isinstance(resolved[contract_id].executor, ResolvedRawAgentExecutor)
    for contract_id in task_ids:
        assert not isinstance(resolved[contract_id].executor, ResolvedRawAgentExecutor)
        assert type(resolved[contract_id].executor).__name__ == "_DeferredTaskExecutor"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ("missing", "missing"),
        ("duplicate", "duplicate"),
        ("extra", "extra"),
        ("wrong-owner", "wrong-owner"),
        ("wrong-adapter", "wrong-adapter"),
        ("contract-digest-drift", "contract-digest-drift"),
    ],
)
def test_raw_binding_rows_reject_invalid_catalog(mutate: str, match: str) -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import (
        RAW_AGENT_RUNTIME_BINDING_ROWS,
        authenticate_raw_agent_runtime_bindings,
    )

    contracts = all_feature_agent_contracts()
    rows = list(RAW_AGENT_RUNTIME_BINDING_ROWS)
    if mutate == "missing":
        rows = rows[1:]
    elif mutate == "duplicate":
        rows.append(rows[0])
    elif mutate == "extra":
        extra = rows[0].model_copy(update={"contract_id": "assurance.intake.agent.forged.v1"})
        rows.append(extra)
    elif mutate == "wrong-owner":
        rows[0] = rows[0].model_copy(update={"runtime_handler_id": "assurance.generation.forged.execute"})
    elif mutate == "wrong-adapter":
        rows[0] = rows[0].model_copy(
            update={"adapter": "cursor", "provider": "cursor", "runtime_handler_id": "runtime.cursor.execute"}
        )
    else:
        rows[0] = rows[0].model_copy(update={"contract_digest": "c" * 64})

    with pytest.raises(ValueError, match=match):
        authenticate_raw_agent_runtime_bindings(rows, contracts, adapter="opencode")
