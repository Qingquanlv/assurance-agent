from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from graph_engine.boot.graph_revision import FeatureFactoryRef

if TYPE_CHECKING:
    from graph_engine.attempts import TaskAttemptContract
    from graph_engine.plugin_kit import CapabilityPlugin


class _OwnedContract(Protocol):
    @property
    def contract_id(self) -> str: ...

    @property
    def owner_id(self) -> str: ...


AgentContractT = TypeVar("AgentContractT", bound=_OwnedContract)


@dataclass(frozen=True, slots=True)
class FeatureSpec(Generic[AgentContractT]):
    """A wheel-owned composition interface; graph topology stays in its factory."""

    plugin: type[CapabilityPlugin]
    agent_contracts: Mapping[str, AgentContractT]
    task_contracts: Mapping[str, TaskAttemptContract[Any, Any]]
    output_route_templates: Mapping[str, tuple[str, ...]]
    graph_factory: FeatureFactoryRef

    def __post_init__(self) -> None:
        owner = self.graph_factory.owner_id
        if self.plugin.spec.plugin_id != owner:
            raise ValueError(f"feature owner mismatch: {owner}")
        agents = dict(self.agent_contracts)
        tasks = dict(self.task_contracts)
        routes = dict(self.output_route_templates)
        agent_ids = tuple(contract.contract_id for contract in agents.values())
        task_ids = tuple(contract.contract_id for contract in tasks.values())
        if (
            len(set(agent_ids)) != len(agent_ids)
            or len(set(task_ids)) != len(task_ids)
            or set(agent_ids) & set(task_ids)
            or any(contract.owner_id != owner for contract in (*agents.values(), *tasks.values()))
        ):
            raise ValueError(f"feature contract identity mismatch: {owner}")
        if not set(agents).issubset(routes) or set(routes) - (set(agents) | set(tasks)):
            raise ValueError(f"feature output route mismatch: {owner}")
        object.__setattr__(self, "agent_contracts", MappingProxyType(agents))
        object.__setattr__(self, "task_contracts", MappingProxyType(tasks))
        object.__setattr__(self, "output_route_templates", MappingProxyType(routes))


__all__ = ["FeatureSpec"]
