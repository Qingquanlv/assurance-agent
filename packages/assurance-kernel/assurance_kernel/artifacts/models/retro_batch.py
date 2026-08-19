"""Typed artifacts for explicit Retro batches and pipeline closure."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

RetroExecutionStatus = Literal[
    "completed",
    "failed",
    "stopped",
    "hard_timeout",
    "cancelled",
    "running",
    "not_started",
]
EvidenceAvailability = Literal["complete", "partial", "absent"]
PersistedRetroResult = Literal["completed", "completed_with_gaps", "pending_reconcile"]
RetroInvocationResultValue = Literal[
    "completed", "completed_with_gaps", "pending_reconcile", "technical_failure"
]


class RetroBatchMember(BaseModel):
    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    execution_status: RetroExecutionStatus
    evidence_availability: EvidenceAvailability


class RetroBatchScope(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    batch_id: str = Field(min_length=1)
    status: Literal["complete", "incomplete"]
    members: tuple[RetroBatchMember, ...] = ()

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        change_ids = tuple(member.change_id for member in self.members)
        if change_ids != tuple(sorted(change_ids)):
            raise ValueError("batch members must use canonical change_id ordering")
        if len(set(change_ids)) != len(change_ids):
            raise ValueError("batch members must be unique by change_id")
        if self.status == "complete" and any(
            member.evidence_availability != "complete" for member in self.members
        ):
            raise ValueError("complete batch requires complete evidence for every member")
        return self


class RetroPipelineFailure(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    failure_id: str = Field(min_length=1)
    retro_id: str = Field(min_length=1)
    batch_id: str | None = None
    stage: str = Field(min_length=1)
    node_id: str | None = None
    error_kind: str = Field(min_length=1)
    message_fingerprint: str = Field(min_length=1)
    runtime_event_id: str | None = None
    occurred_at: datetime


class RetroPipelineFailureDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    failures: tuple[RetroPipelineFailure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failures(self) -> Self:
        failure_ids = tuple(failure.failure_id for failure in self.failures)
        if failure_ids != tuple(sorted(failure_ids)) or len(set(failure_ids)) != len(failure_ids):
            raise ValueError("pipeline failures must be sorted and unique by failure_id")
        if any(failure.retro_id != self.retro_id for failure in self.failures):
            raise ValueError("pipeline failure retro_id must match its document")
        return self


class RetroRunStatus(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    batch_id: str | None = None
    result: PersistedRetroResult
    improvement_ids: tuple[str, ...] = ()
    outbox_id: str | None = None
    failure_ids: tuple[str, ...] = ()


class RetroInvocationResult(BaseModel):
    model_config = _FROZEN

    status: RetroRunStatus | None
    result: RetroInvocationResultValue

    @model_validator(mode="after")
    def validate_status_binding(self) -> Self:
        if self.result == "technical_failure":
            if self.status is not None:
                raise ValueError("technical_failure cannot have a persisted Retro status")
            return self
        if self.status is None or self.status.result != self.result:
            raise ValueError("successful invocation result must match persisted Retro status")
        return self


__all__ = [
    "EvidenceAvailability",
    "PersistedRetroResult",
    "RetroBatchMember",
    "RetroBatchScope",
    "RetroExecutionStatus",
    "RetroInvocationResult",
    "RetroInvocationResultValue",
    "RetroPipelineFailure",
    "RetroPipelineFailureDocument",
    "RetroRunStatus",
]
