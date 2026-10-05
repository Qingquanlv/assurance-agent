from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import Field

from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import FrozenModel, TaskWorkspaceBinding


class AttemptExecutionContext(FrozenModel):
    invocation_id: str = Field(min_length=1)
    public_entrypoint: str = Field(min_length=1)
    semantic_node_id: str = Field(min_length=1)
    attempt_key: AttemptKey
    fencing_token: int = Field(ge=1)
    authorization_id: str | None = Field(default=None, min_length=1)
    seed_attempt_key: AttemptKey | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedAttemptScope:
    execution: AttemptExecutionContext
    workspace: TaskWorkspaceBinding
    runtime_evidence: Callable[[], Awaitable[JSONValue]] | None = None

    async def read_runtime_evidence(self) -> JSONValue:
        """Organized evidence for this invocation, or an error when it was not declared."""
        if self.runtime_evidence is None:
            raise GraphEngineError("runtime evidence is not declared on this contract")
        return await self.runtime_evidence()


__all__ = ["AttemptExecutionContext", "AuthorizedAttemptScope"]
