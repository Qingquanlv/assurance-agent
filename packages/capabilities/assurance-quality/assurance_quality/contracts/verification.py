"""Deterministic business verdicts derived from authenticated runtime facts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.execution_plan import ValidationProfile

VerificationStatus = Literal["PASSED", "FAILED", "INCOMPLETE"]
BusinessStatus = Literal["satisfied", "violated", "not_applicable", "not_evaluated"]
EvidenceStatus = Literal["observed", "missing", "error", "timeout", "skipped"]


class VerificationObligationV1(FrozenModel):
    obligation_id: str = Field(min_length=1)
    kind: Literal["business", "completion"]
    evidence_status: EvidenceStatus
    business_status: BusinessStatus
    expected: FrozenJSONValue = None
    actual: FrozenJSONValue = None
    reason: str | None = Field(default=None, min_length=1)


class VerificationVerdictV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    validation_profile: ValidationProfile
    execution_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    case_id: str = Field(min_length=1)
    verdict: VerificationStatus
    required: int = Field(ge=0)
    executed: int = Field(ge=0)
    evaluated: int = Field(ge=0)
    satisfied: int = Field(ge=0)
    obligations: tuple[VerificationObligationV1, ...]
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _closed_counts(self) -> Self:
        ids = tuple(item.obligation_id for item in self.obligations)
        if not ids or ids != tuple(sorted(set(ids))):
            raise ValueError("required obligations must be non-empty, sorted, and unique")
        if self.required != len(ids):
            raise ValueError("required count must include every obligation")
        derived = (
            sum(item.evidence_status == "observed" for item in self.obligations),
            sum(
                item.business_status in {"satisfied", "violated", "not_applicable"}
                for item in self.obligations
            ),
            sum(item.business_status in {"satisfied", "not_applicable"} for item in self.obligations),
        )
        if (self.executed, self.evaluated, self.satisfied) != derived:
            raise ValueError("verification counts do not match obligation facts")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("verification reason codes must be sorted and unique")
        for item in self.obligations:
            if (item.evidence_status == "observed") == (item.business_status == "not_evaluated"):
                raise ValueError("verification evidence and evaluation statuses contradict")
        business_violated = any(
            item.kind == "business" and item.business_status == "violated" for item in self.obligations
        )
        unavailable = any(item.evidence_status != "observed" for item in self.obligations)
        completion_unsatisfied = any(
            item.kind == "completion" and item.business_status == "violated" for item in self.obligations
        )
        runner_incomplete = "verification.runner_incomplete" in self.reason_codes
        derived_verdict: VerificationStatus = (
            "FAILED"
            if business_violated
            else ("INCOMPLETE" if unavailable or completion_unsatisfied or runner_incomplete else "PASSED")
        )
        if self.verdict != derived_verdict:
            raise ValueError("verification verdict contradicts obligation and completion facts")
        reason_facts = {
            "verification.business_violation": business_violated,
            "verification.required_evidence_missing": unavailable,
            "verification.completion_unsatisfied": completion_unsatisfied,
        }
        for reason, present in reason_facts.items():
            if (reason in self.reason_codes) != present:
                raise ValueError("verification reason codes contradict obligation facts")
        return self

    def by_id(self, obligation_id: str) -> VerificationObligationV1:
        for item in self.obligations:
            if item.obligation_id == obligation_id:
                return item
        raise KeyError(obligation_id)


__all__ = [
    "BusinessStatus",
    "EvidenceStatus",
    "VerificationObligationV1",
    "VerificationStatus",
    "VerificationVerdictV1",
]
