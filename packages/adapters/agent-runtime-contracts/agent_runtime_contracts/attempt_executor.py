from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from graph_engine.attempts import (
    AttemptExecutionContext,
    IndeterminateTaskResult,
    PermanentTaskFailure,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)

from agent_runtime_contracts.execution_contract import AgentExecutionContract
from agent_runtime_contracts.models import AgentRunResult
from agent_runtime_contracts.schema import thaw_json, validate_local_agent_result


InputT = TypeVar("InputT", bound=BaseModel)
PreparedT = TypeVar("PreparedT")
AgentResultT = TypeVar("AgentResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)
_PreparedT_contra = TypeVar("_PreparedT_contra", contravariant=True)
_PreparedT_co = TypeVar("_PreparedT_co", covariant=True)
_OutputT_co = TypeVar("_OutputT_co", bound=BaseModel, covariant=True)


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


@dataclass(frozen=True, slots=True)
class ReadOnlyRawWorkspace:
    root: Path

    def _resolved(self, relative: str) -> Path:
        if not _canonical_relative(relative):
            raise ValueError(f"raw workspace path must be canonical and relative: {relative}")
        path = self.root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"raw workspace path is a symlink: {relative}")
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise ValueError(
                f"raw workspace path must stay inside the authorized root: {relative}"
            ) from error
        return path

    def read_bytes(self, relative: str) -> bytes:
        path = self._resolved(relative)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative)
        return path.read_bytes()

    def read_text(self, relative: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative).decode(encoding)


@dataclass(frozen=True, slots=True)
class RawAgentRuntimeOutcome:
    run_result: AgentRunResult
    raw_workspace: ReadOnlyRawWorkspace


@dataclass(frozen=True, slots=True)
class RawFinalizeBundle(Generic[InputT, PreparedT, AgentResultT]):
    validated_input: InputT
    prepared: PreparedT
    agent_result: AgentResultT
    run_evidence: AgentRunResult
    raw_workspace: ReadOnlyRawWorkspace


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
    ) -> RawAgentRuntimeOutcome: ...


class FinalizePhase(Protocol[InputT, PreparedT, AgentResultT, _OutputT_co]):
    async def execute(
        self,
        bundle: RawFinalizeBundle[InputT, PreparedT, AgentResultT],
        context: AttemptExecutionContext,
    ) -> _OutputT_co: ...


class ResolvedRawAgentExecutor(Generic[InputT, PreparedT, AgentResultT, OutputT]):
    def __init__(
        self,
        contract: AgentExecutionContract[InputT, AgentResultT, OutputT],
        *,
        prepare: PreparePhase[InputT, PreparedT],
        runtime: RuntimePhase[PreparedT],
        finalize: FinalizePhase[InputT, PreparedT, AgentResultT, OutputT],
        result_context: Mapping[str, object] | None = None,
    ) -> None:
        self._contract = contract
        self._prepare = prepare
        self._runtime = runtime
        self._finalize = finalize
        self._result_context = None if result_context is None else dict(result_context)

    def to_task_contract(self) -> TaskAttemptContract[InputT, OutputT]:
        return self._contract.to_task_contract()

    def resolve(self) -> ResolvedAttemptContract[InputT, OutputT]:
        return resolve_contract(self._contract.to_task_contract(), executor=self)

    async def execute(
        self,
        validated_input: InputT,
        context: AttemptExecutionContext,
    ) -> OutputT:
        prepared = await self._prepare.execute(validated_input, context)
        outcome = await self._runtime.execute(prepared, context)
        return await self._finalize_outcome(validated_input, prepared, outcome, context)

    async def reconcile(
        self,
        validated_input: InputT,
        context: AttemptExecutionContext,
        snapshot: object,
    ) -> OutputT | IndeterminateTaskResult | PermanentTaskFailure:
        prepared = await self._prepare.execute(validated_input, context)
        runtime_reconcile = getattr(self._runtime, "reconcile", None)
        if runtime_reconcile is None:
            return PermanentTaskFailure(
                kind="internal",
                message="in-flight activity cannot be adopted",
            )
        outcome = await runtime_reconcile(prepared, context, snapshot)
        if isinstance(outcome, (IndeterminateTaskResult, PermanentTaskFailure)):
            return outcome
        return await self._finalize_outcome(validated_input, prepared, outcome, context)

    async def _finalize_outcome(
        self,
        validated_input: InputT,
        prepared: PreparedT,
        outcome: RawAgentRuntimeOutcome,
        context: AttemptExecutionContext,
    ) -> OutputT:
        _exact, _digest, agent_result = validate_local_agent_result(
            thaw_json(outcome.run_result.result_payload),
            result_model=self._contract.agent_result_model,
            context=self._result_context,
        )
        bundle = RawFinalizeBundle(
            validated_input=validated_input,
            prepared=prepared,
            agent_result=agent_result,
            run_evidence=outcome.run_result,
            raw_workspace=outcome.raw_workspace,
        )
        output = await self._finalize.execute(bundle, context)
        return self._contract.output_model.model_validate(output)


__all__ = [
    "FinalizePhase",
    "PreparePhase",
    "RawAgentRuntimeOutcome",
    "RawFinalizeBundle",
    "ReadOnlyRawWorkspace",
    "ResolvedRawAgentExecutor",
    "RuntimePhase",
]
