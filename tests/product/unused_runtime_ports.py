from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from graph_engine.plugin_api import WorkspaceProvider

from graph_engine.application.runtime_context import (
    AttemptKernelPort,
    SecretResolverPort,
)
from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.contracts import ResolvedAttemptContract
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import AttemptResolution
from graph_engine.plugin_api import (
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedWriteSet,
    TaskWorkspaceBinding,
)


class UnusedSecretResolver:
    def resolve(self, handle: str) -> bytes:
        raise RuntimeError(f"secret resolver is unused: {handle}")


class UnusedWorkspaceProvider:
    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims, *, seed_from: AttemptKey | None = None
    ) -> TaskWorkspaceBinding:
        raise RuntimeError("workspace provider is unused")

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        raise RuntimeError("workspace provider is unused")

    async def prepare(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        raise RuntimeError("workspace provider is unused")

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        raise RuntimeError("workspace provider is unused")

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        raise RuntimeError("workspace provider is unused")


class UnusedAttemptKernel:
    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        raise RuntimeError("attempt kernel is unused")


UNUSED_SECRET_RESOLVER: SecretResolverPort = UnusedSecretResolver()
UNUSED_WORKSPACE_PROVIDER: WorkspaceProvider = UnusedWorkspaceProvider()
UNUSED_ATTEMPT_KERNEL: AttemptKernelPort = UnusedAttemptKernel()
