from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from graph_engine.attempts import (
    AttemptExecutionContext,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)

from agent_runtime_contracts.execution_contract import AgentExecutionContract
from agent_runtime_contracts.plugin_kit import StructuredOutputCapabilityError, negotiate_provider_schema
from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities


InputT = TypeVar("InputT", bound=BaseModel)
PreparedT = TypeVar("PreparedT")
AgentResultT = TypeVar("AgentResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)
_PreparedT_contra = TypeVar("_PreparedT_contra", contravariant=True)
_PreparedT_co = TypeVar("_PreparedT_co", covariant=True)
_OutputT_co = TypeVar("_OutputT_co", bound=BaseModel, covariant=True)


@dataclass(frozen=True, slots=True)
class TypedPhaseBundle(Generic[PreparedT, AgentResultT]):
    prepared: PreparedT
    agent_result: AgentResultT


class PreparePhase(Protocol[_InputT_contra, _PreparedT_co]):
    async def execute(
        self,
        validated_input: _InputT_contra,
        context: AttemptExecutionContext,
    ) -> _PreparedT_co: ...


class RuntimePhase(Protocol[_PreparedT_contra]):
    async def execute(
        self,
        prepared: _PreparedT_contra,
        context: AttemptExecutionContext,
        *,
        schema: Mapping[str, Any],
    ) -> object: ...


class FinalizePhase(Protocol[PreparedT, AgentResultT, _OutputT_co]):
    async def execute(
        self,
        bundle: TypedPhaseBundle[PreparedT, AgentResultT],
        context: AttemptExecutionContext,
    ) -> _OutputT_co: ...


class CompositeAttemptExecutor(Generic[InputT, PreparedT, AgentResultT, OutputT]):
    def __init__(
        self,
        contract: AgentExecutionContract[InputT, AgentResultT, OutputT],
        *,
        prepare: PreparePhase[InputT, PreparedT],
        runtime: RuntimePhase[PreparedT],
        finalize: FinalizePhase[PreparedT, AgentResultT, OutputT],
        capabilities: AgentRuntimeCapabilities,
    ) -> None:
        self._contract = contract
        self._prepare = prepare
        self._runtime = runtime
        self._finalize = finalize
        self._capabilities = capabilities

    def to_task_contract(self) -> TaskAttemptContract[InputT, OutputT]:
        return self._contract.to_task_contract()

    def resolve(self) -> ResolvedAttemptContract[InputT, OutputT]:
        return resolve_contract(self._contract.to_task_contract(), executor=self)

    async def execute(
        self,
        validated_input: InputT,
        context: AttemptExecutionContext,
    ) -> OutputT:
        negotiate_provider_schema(
            required=self._contract.requires_provider_schema,
            capabilities=self._capabilities,
        )
        prepared = await self._prepare.execute(validated_input, context)
        schema = self._contract.agent_result_model.model_json_schema()
        raw_result = await self._runtime.execute(prepared, context, schema=schema)
        agent_result = self._contract.agent_result_model.model_validate(raw_result)
        bundle = TypedPhaseBundle(prepared=prepared, agent_result=agent_result)
        output = await self._finalize.execute(bundle, context)
        return self._contract.output_model.model_validate(output)


__all__ = [
    "CompositeAttemptExecutor",
    "PreparePhase",
    "RuntimePhase",
    "FinalizePhase",
    "StructuredOutputCapabilityError",
    "TypedPhaseBundle",
]
