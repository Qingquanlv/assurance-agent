from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol

from pydantic import BaseModel

from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.contracts import ResolvedAttemptContract
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import AttemptResolution
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedWriteSet,
    TaskWorkspaceBinding,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AttemptKernelPort(Protocol):
    """Per-invocation Kernel handle. Held by reference; never checkpointed."""

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution: ...


class SecretResolverPort(Protocol):
    """Per-invocation secret resolver. Held by reference; never checkpointed."""

    def resolve(self, handle: str) -> bytes: ...


class WorkspaceProviderPort(Protocol):
    """Per-invocation workspace provider. Held by reference; never checkpointed."""

    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims, *, seed_from: AttemptKey | None = None
    ) -> TaskWorkspaceBinding: ...

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet: ...

    async def prepare(
        self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet
    ) -> PreparedWorkspaceRef: ...

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...


def _revision_id(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("revision id must be a lowercase SHA-256 hex value")
    return value


def _fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class AssuranceRuntimeContext:
    revision_id: str
    fencing_token: int
    attempt_kernel: AttemptKernelPort
    secret_resolver: SecretResolverPort
    workspace_provider: WorkspaceProviderPort

    def __post_init__(self) -> None:
        _revision_id(self.revision_id)
        _fencing_token(self.fencing_token)

    def checkpoint_projection(self) -> dict[str, JSONValue]:
        return {
            "revision_id": self.revision_id,
            "fencing_token": self.fencing_token,
        }


__all__ = [
    "AssuranceRuntimeContext",
    "AttemptKernelPort",
    "SecretResolverPort",
    "WorkspaceProviderPort",
]
