from __future__ import annotations

from typing import Generic, Literal, TypeAlias, TypeVar

from pydantic import Field

from graph_engine.artifacts import ArtifactRef
from graph_engine.plugin_api import FailureKind, FrozenModel


OutputT = TypeVar("OutputT")


class ReceiptRef(FrozenModel):
    receipt_id: str = Field(min_length=1)
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SystemReference(FrozenModel):
    reference_id: str = Field(min_length=1)


class CommittedTaskResult(FrozenModel, Generic[OutputT]):
    output: OutputT
    receipt: ReceiptRef
    # Sealed write-set refs. Empty when a caller built the result without a kernel seal.
    committed_artifacts: tuple[ArtifactRef, ...] = ()


class RejectedTaskResult(FrozenModel):
    reason: str = Field(min_length=1)
    writes_promoted: Literal[False] = False


class PermanentTaskFailure(FrozenModel):
    kind: FailureKind
    message: str = Field(min_length=1)
    retryable: bool = False
    writes_promoted: Literal[False] = False


class PendingTaskResult(FrozenModel):
    wakeup: SystemReference


class IndeterminateTaskResult(FrozenModel):
    reconciliation: SystemReference


AttemptResolution: TypeAlias = (
    CommittedTaskResult
    | RejectedTaskResult
    | PermanentTaskFailure
    | PendingTaskResult
    | IndeterminateTaskResult
)


__all__ = [
    "AttemptResolution",
    "CommittedTaskResult",
    "IndeterminateTaskResult",
    "PendingTaskResult",
    "PermanentTaskFailure",
    "ReceiptRef",
    "RejectedTaskResult",
    "SystemReference",
]
