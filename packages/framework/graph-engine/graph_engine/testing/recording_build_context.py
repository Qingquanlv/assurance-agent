from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from graph_engine.attempts.models.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.orchestration.node_factory import AttemptNodeFactory
from graph_engine.artifacts import ArtifactRef
from graph_engine.boot.boot import BoundAttemptNode, ContractOwnershipError
from graph_engine.stategraph.publish import call_publish
from graph_engine.boot.graph_revision import GraphBuildManifest
from graph_engine.composition.models import AttemptContractRegistry
from graph_engine.plugin_api import PluginContribution


def _owner_id(contract: object) -> str:
    if isinstance(contract, ResolvedAttemptContract):
        return contract.contract.owner_id
    owner_id = getattr(contract, "owner_id", None)
    if isinstance(owner_id, str):
        return owner_id
    raise ContractOwnershipError("contract owner is missing")


def _contract_id(contract: object) -> str:
    if isinstance(contract, ResolvedAttemptContract):
        return contract.contract.contract_id
    contract_id = getattr(contract, "contract_id", None)
    if isinstance(contract_id, str):
        return contract_id
    raise ContractOwnershipError("contract id is missing")


class RecordingCapabilityBuildContext:
    def __init__(
        self,
        *,
        owner_id: str,
        contracts: Mapping[str, TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any]],
        attempt_factory: AttemptNodeFactory | None = None,
        recorder: _TraceRecorder | None = None,
        catalog: AttemptContractRegistry | None = None,
        contribution: PluginContribution | None = None,
        manifest: GraphBuildManifest | None = None,
    ) -> None:
        if not owner_id:
            raise ValueError("capability owner id must be nonempty")
        self.owner_id = owner_id
        self._contracts = dict(contracts)
        self._attempt_factory = attempt_factory
        self._recorder = recorder
        self.catalog = catalog
        self.contribution = contribution
        self.manifest = manifest
        self._reused_handlers: dict[str, object] = {}
        self._bound_contract_ids: list[str] = []
        self._compiled_subgraph_checkpointers: list[object] = []
        self._select_values: list[object] = []
        self._published_updates: list[Mapping[str, object]] = []

    @property
    def bound_contract_ids(self) -> tuple[str, ...]:
        return tuple(self._bound_contract_ids)

    @property
    def compiled_subgraph_checkpointers(self) -> tuple[object, ...]:
        return tuple(self._compiled_subgraph_checkpointers)

    @property
    def select_values(self) -> tuple[object, ...]:
        return tuple(self._select_values)

    @property
    def published_updates(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._published_updates)

    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> Any:
        contract = self._contracts.get(contract_id)
        if contract is None or _owner_id(contract) != self.owner_id:
            raise ContractOwnershipError(f"contract {contract_id!r} is not owned by {self.owner_id!r}")
        self._bound_contract_ids.append(contract_id)
        recording_select = self._record_select(select)
        recording_publish = self._record_publish(publish)
        if self._attempt_factory is None:
            del activation, recording_select, recording_publish
            return BoundAttemptNode(
                contract_id=contract_id,
                semantic_node_id=semantic_node_id,
                owner_id=self.owner_id,
            )
        return self._attempt_factory.attempt(
            contract,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=recording_select,
            publish=recording_publish,
        )

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph:
        compiled = builder.compile(checkpointer=None)
        self._compiled_subgraph_checkpointers.append(compiled.checkpointer)
        return compiled

    def reused_registry_handler(self, contract_id: str) -> object:
        return self._reused_handlers[contract_id]

    def with_test_contract(
        self,
        contract: TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any],
    ) -> RecordingCapabilityBuildContext:
        if _owner_id(contract) != self.owner_id:
            raise ContractOwnershipError(
                f"contract {_contract_id(contract)!r} is not owned by {self.owner_id!r}"
            )
        catalog = self.catalog
        if catalog is None:
            raise TypeError("with_test_contract requires an authenticated catalog")
        task = contract.contract if isinstance(contract, ResolvedAttemptContract) else contract
        entry = next(
            (item for item in catalog.entries.values() if item.handler_id == task.handler_id),
            None,
        )
        if entry is None:
            raise ContractOwnershipError(
                f"handler {task.handler_id!r} is not an authenticated registry entry"
            )
        authenticated_validators = {
            validator_id for item in catalog.entries.values() for validator_id in item.validators
        }
        for validator_id in task.validators:
            if validator_id not in authenticated_validators:
                raise ContractOwnershipError(
                    f"validator {validator_id!r} is not an authenticated registry entry"
                )
        installed = dict(self._contracts)
        contract_id = _contract_id(contract)
        installed[contract_id] = contract
        clone = RecordingCapabilityBuildContext(
            owner_id=self.owner_id,
            contracts=installed,
            attempt_factory=self._attempt_factory,
            recorder=self._recorder,
            catalog=catalog,
            contribution=self.contribution,
            manifest=self.manifest,
        )
        clone._reused_handlers = dict(self._reused_handlers)
        clone._reused_handlers[contract_id] = entry.authority_handler
        return clone

    def _record_select(self, select: object) -> Callable[..., object]:
        if not callable(select):
            raise TypeError("select must be callable")

        def wrapped(state: object) -> object:
            value = select(state)
            recorded: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            self._select_values.append(recorded)
            if self._recorder is not None:
                self._recorder.select_values.append(recorded)
            return value

        return wrapped

    def _record_publish(self, publish: object) -> Callable[..., Mapping[str, object]]:
        if not callable(publish):
            raise TypeError("publish must be callable")

        def wrapped(
            state: object,
            output: object,
            receipt: object,
            *,
            committed: tuple[ArtifactRef | Mapping[str, object], ...] = (),
        ) -> Mapping[str, object]:
            update = call_publish(
                publish,
                state if isinstance(state, Mapping) else {},
                output,
                receipt,
                committed,
            )
            published = {str(name): value for name, value in update.items()}
            self._published_updates.append(published)
            if self._recorder is not None:
                self._recorder.published_updates.append(published)
            return published

        return wrapped


class _TraceRecorder:
    def __init__(self) -> None:
        self.select_values: list[object] = []
        self.published_updates: list[Mapping[str, object]] = []


__all__ = ["RecordingCapabilityBuildContext"]
