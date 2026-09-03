"""Data-only improvement effect intent and receipt contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_improvement.contracts.declarations import DeclarationProposalReceipt
from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    KnowledgeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.promotion import PromotionReceipt
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

EffectKind = Literal[
    "change_export",
    "knowledge_export",
    "memory_eval",
    "memory_apply",
    "memory_rollback",
    "test_promotion",
    "declaration_write",
    "archive",
]

_DELIVERY_KINDS = frozenset(
    {
        "change_export",
        "knowledge_export",
        "memory_eval",
        "memory_apply",
        "memory_rollback",
        "declaration_write",
    }
)


class ArchiveApplyReceipt(BaseModel):
    model_config = _FROZEN

    invocation_id: NonEmptyStr
    archive_digest: NonEmptyStr
    summary_path: NonEmptyStr | None = None


class ImprovementEffectIntentV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    kind: EffectKind
    improvement_id: NonEmptyStr
    target: NonEmptyStr | None = None
    artifact_path: NonEmptyStr | None = None
    candidate_id: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None
    version: int | None = None
    target_kind: NonEmptyStr | None = None
    target_digest: NonEmptyStr | None = None
    promotion_digest: NonEmptyStr | None = None
    invocation_id: NonEmptyStr | None = None
    archive_digest: NonEmptyStr | None = None
    change_export: ChangeExportReceipt | None = None
    knowledge_export: KnowledgeExportReceipt | None = None
    memory_eval: MemoryEvalReceipt | None = None
    memory_apply: MemoryApplyReceipt | None = None
    memory_rollback: MemoryRollbackReceipt | None = None
    promotion: PromotionReceipt | None = None
    declaration: DeclarationProposalReceipt | None = None

    @model_validator(mode="after")
    def validate_key_fields(self) -> Self:
        if self.kind == "test_promotion":
            if self.version is None or self.promotion_digest is None:
                raise ValueError("promotion intent requires version and promotion_digest")
        elif self.kind == "archive":
            if self.invocation_id is None or self.archive_digest is None:
                raise ValueError("archive intent requires invocation_id and archive_digest")
        elif self.kind in _DELIVERY_KINDS:
            if self.version is None or self.target_kind is None or self.target_digest is None:
                raise ValueError("delivery intent requires version, target_kind, and target_digest")
        return self


class ImprovementEffectReceiptV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    kind: EffectKind
    improvement_id: NonEmptyStr
    settlement_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_export: ChangeExportReceipt | None = None
    knowledge_export: KnowledgeExportReceipt | None = None
    memory_eval: MemoryEvalReceipt | None = None
    memory_apply: MemoryApplyReceipt | None = None
    memory_rollback: MemoryRollbackReceipt | None = None
    promotion: PromotionReceipt | None = None
    declaration: DeclarationProposalReceipt | None = None
    archive: ArchiveApplyReceipt | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> Self:
        payloads = {
            "change_export": self.change_export,
            "knowledge_export": self.knowledge_export,
            "memory_eval": self.memory_eval,
            "memory_apply": self.memory_apply,
            "memory_rollback": self.memory_rollback,
            "test_promotion": self.promotion,
            "declaration_write": self.declaration,
            "archive": self.archive,
        }
        present = tuple(name for name, value in payloads.items() if value is not None)
        if present != (self.kind,):
            raise ValueError("effect receipt payload must match kind")
        return self
