from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any, cast

from graph_engine.attempts import TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef


def _agent_catalog() -> tuple[Mapping[str, Any], Mapping[str, tuple[str, ...]]]:
    from assurance_healing.contracts.coverage_repair import CoverageRepairStatus
    from assurance_healing.ops import router

    # The published coverage-repair models stay CoverageRepairStatus. The handler
    # still checks CoverageRepairApplySummary, which the executor rejects before
    # finalize. See test_coverage_repair_handler_summary_does_not_match_raw_contract.
    contracts = {
        name: (
            replace(
                contract,
                agent_result_model=CoverageRepairStatus,
                output_model=CoverageRepairStatus,
            )
            if name == "coverage-repair"
            else contract
        )
        for name, contract in router.agent_contracts().items()
    }
    return MappingProxyType(contracts), router.output_routes()


AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType({})
HEALING_EFFECT_IDS: tuple[str, ...] = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.healing.effect.proposal-approved.v1",
)


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in AGENT_JOB_CONTRACTS.values()
            ),
            key=lambda item: item.contract_id,
        )
    )


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "HEALING_EFFECT_IDS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
