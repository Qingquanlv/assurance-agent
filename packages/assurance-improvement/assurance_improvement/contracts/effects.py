"""Data-only improvement effect intent and receipt contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

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
]


class ImprovementEffectIntentV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    kind: EffectKind
    improvement_id: NonEmptyStr
    target: NonEmptyStr | None = None
    artifact_path: NonEmptyStr | None = None
    candidate_id: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None


class ImprovementEffectReceiptV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    kind: EffectKind
    improvement_id: NonEmptyStr
    change_export: ChangeExportReceipt | None = None
    knowledge_export: KnowledgeExportReceipt | None = None
    memory_eval: MemoryEvalReceipt | None = None
    memory_apply: MemoryApplyReceipt | None = None
    memory_rollback: MemoryRollbackReceipt | None = None
    promotion: PromotionReceipt | None = None
    declaration: DeclarationProposalReceipt | None = None

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
        }
        present = tuple(name for name, value in payloads.items() if value is not None)
        if present != (self.kind,):
            raise ValueError("effect receipt payload must match kind")
        return self
