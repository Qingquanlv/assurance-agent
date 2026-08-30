from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from graph_engine.plugin_api import FrozenModel, ResourceClaimTemplate, ResourceClaims


class AgentExecutionContract(FrozenModel):
    contract_id: str
    skill_id: str
    agent_profile: str
    resources: ResourceClaims | ResourceClaimTemplate


def _feature_and_base(contract: AgentExecutionContract) -> tuple[str, str]:
    body = contract.contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, base = body.partition(".agent.")
    if marker != ".agent." or not feature or not base:
        raise ValueError(f"invalid agent job contract id: {contract.contract_id!r}")
    return feature, base


def expand_agent_job_slots(
    catalogs: Sequence[Mapping[str, AgentExecutionContract]],
) -> Mapping[str, AgentExecutionContract]:
    expanded: dict[str, AgentExecutionContract] = {}
    for catalog in catalogs:
        for base, contract in catalog.items():
            feature, contract_base = _feature_and_base(contract)
            if contract_base != base:
                raise ValueError(f"agent job catalog key drifted: {base!r} vs {contract.contract_id!r}")
            for phase in ("prepare", "execute", "finalize"):
                alias = f"assurance.product.agent.{feature}.{base}.{phase}"
                if alias in expanded:
                    raise ValueError(f"duplicate agent job alias: {alias}")
                expanded[alias] = contract
    return MappingProxyType(expanded)


__all__ = [
    "AgentExecutionContract",
    "expand_agent_job_slots",
]
