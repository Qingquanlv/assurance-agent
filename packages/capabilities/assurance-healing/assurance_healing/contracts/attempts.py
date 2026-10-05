from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from graph_engine.attempts import TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef


def _agent_catalog() -> tuple[Mapping[str, Any], Mapping[str, tuple[str, ...]]]:
    from assurance_healing.ops import router

    return MappingProxyType(router.agent_contracts()), router.output_routes()


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
