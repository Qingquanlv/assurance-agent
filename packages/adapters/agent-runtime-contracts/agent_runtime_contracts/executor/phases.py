from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from graph_engine.attempts import (
    AttemptKey,
    AuthorizedAttemptScope,
    PermanentTaskFailure,
)

from agent_runtime_contracts.runtime.protocol import ReadOnlyRawWorkspace
from agent_runtime_contracts.wire.models import AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest

InputT = TypeVar("InputT", bound=BaseModel)
PreparedT = TypeVar("PreparedT")
AgentResultT = TypeVar("AgentResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)
_PreparedT_co = TypeVar("_PreparedT_co", covariant=True)
_OutputT_co = TypeVar("_OutputT_co", bound=BaseModel, covariant=True)


def phase_task_id(attempt_key: AttemptKey, phase: str, handler_id: str) -> str:
    return canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "handler_id": handler_id,
            "phase": phase,
        }
    )


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
        scope: AuthorizedAttemptScope,
    ) -> _PreparedT_co | PermanentTaskFailure: ...


class FinalizePhase(Protocol[InputT, PreparedT, AgentResultT, _OutputT_co]):
    async def execute(
        self,
        bundle: RawFinalizeBundle[InputT, PreparedT, AgentResultT],
        scope: AuthorizedAttemptScope,
    ) -> _OutputT_co | PermanentTaskFailure: ...
