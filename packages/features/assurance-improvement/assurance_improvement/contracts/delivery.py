"""Data-only delivery receipts, outbox entry, and delivery document."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_improvement.contracts.improvements import DeliveryKind, ImprovementCandidateV3
from assurance_improvement.contracts.retro import RetroContextV3, RetroPipelineFailure
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")
EvalOutcome = Literal["passed", "regressed", "awaiting_baseline", "error"]


def artifact_digest(model: BaseModel) -> str:
    payload = model.model_dump(mode="json")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def digest_hex(value: str) -> str:
    return value.removeprefix("sha256:")


def same_digest(left: str, right: str) -> bool:
    return digest_hex(left) == digest_hex(right)


class ChangeExportReceipt(BaseModel):
    model_config = _FROZEN

    sha256: NonEmptyStr
    created: bool
    artifact_path: NonEmptyStr


class KnowledgeExportReceipt(BaseModel):
    model_config = _FROZEN

    sha256: NonEmptyStr
    created: bool
    artifact_path: NonEmptyStr


class MemoryEvalReceipt(BaseModel):
    model_config = _FROZEN

    eval_run_id: NonEmptyStr
    outcome: EvalOutcome
    report_sha256: NonEmptyStr
    staged_sha256: NonEmptyStr
    baseline_sha256: NonEmptyStr | None = None
    approved_state_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    approved_version: int | None = Field(default=None, ge=1)


class ImprovementApplyProof(BaseModel):
    model_config = _FROZEN

    approved_state_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_version: int = Field(ge=1)
    evaluation: MemoryEvalReceipt

    @model_validator(mode="after")
    def validate_current_evaluation(self) -> Self:
        if self.evaluation.outcome != "passed":
            raise ValueError("apply proof requires a passed evaluation")
        if self.evaluation.approved_version not in {None, self.approved_version}:
            raise ValueError("evaluation receipt version is stale")
        if self.evaluation.approved_state_digest not in {None, self.approved_state_digest}:
            raise ValueError("evaluation receipt digest does not match approved state")
        return self


class ApplyAttemptResult(BaseModel):
    model_config = _FROZEN

    applied: bool
    effect_intents: tuple[object, ...] = ()
    write_authorization: tuple[str, ...] = ()


class MemoryApplyReceipt(BaseModel):
    model_config = _FROZEN

    target: NonEmptyStr
    before_sha256: NonEmptyStr
    after_sha256: NonEmptyStr
    receipt_sha256: NonEmptyStr


class MemoryRollbackReceipt(BaseModel):
    model_config = _FROZEN

    target: NonEmptyStr
    restored_sha256: NonEmptyStr
    reason: NonEmptyStr


class ImprovementOutboxEntry(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context: RetroContextV3
    candidate: ImprovementCandidateV3
    pipeline_failure: RetroPipelineFailure | None = None

    @model_validator(mode="after")
    def validate_embedded_digests(self) -> Self:
        if self.retro_id != self.context.retro_id:
            raise ValueError("outbox retro_id must match embedded context")
        if self.candidate_sha256 != artifact_digest(self.candidate):
            raise ValueError("outbox candidate_sha256 mismatch")
        if self.context_sha256 != artifact_digest(self.context):
            raise ValueError("outbox context_sha256 mismatch")
        if self.pipeline_failure is not None and self.pipeline_failure.retro_id != self.retro_id:
            raise ValueError("outbox pipeline failure retro_id mismatch")
        return self


class ImprovementDeliveryDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    improvement_id: NonEmptyStr
    expected_improvement_version: int = Field(ge=1)
    delivery: DeliveryKind
    change_export: ChangeExportReceipt | None = None
    knowledge_export: KnowledgeExportReceipt | None = None
    memory_eval: MemoryEvalReceipt | None = None
    memory_apply: MemoryApplyReceipt | None = None
    memory_rollback: MemoryRollbackReceipt | None = None

    @model_validator(mode="after")
    def validate_delivery_receipt(self) -> Self:
        present = tuple(
            name
            for name, value in (
                ("change_export", self.change_export),
                ("knowledge_export", self.knowledge_export),
                ("memory_eval", self.memory_eval),
                ("memory_apply", self.memory_apply),
                ("memory_rollback", self.memory_rollback),
            )
            if value is not None
        )
        expected = {
            DeliveryKind.CHANGE_DRAFT: ("change_export",),
            DeliveryKind.KNOWLEDGE_DELTA: ("knowledge_export",),
            DeliveryKind.MEMORY_PATCH: ("memory_eval", "memory_apply", "memory_rollback"),
            DeliveryKind.TEST_PROMOTION: (),
        }[self.delivery]
        if self.delivery is DeliveryKind.TEST_PROMOTION:
            if present:
                raise ValueError("test_promotion delivery must not carry memory/export receipts")
            return self
        if not present or any(name not in expected for name in present):
            raise ValueError(f"{self.delivery} delivery receipt does not match delivery kind")
        return self
