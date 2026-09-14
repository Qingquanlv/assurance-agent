"""Review coverage and source observations; semantic judgments remain the reviewer's."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_intake.contracts import EvidenceArtifactRefV1, NonEmptyStr

CheckResult = Literal["pass", "finding", "not_applicable"]


class CaseReviewChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: CheckResult
    auth: CheckResult
    setup: CheckResult
    assertion: CheckResult
    cleanup: CheckResult
    helpers: CheckResult


class CaseReviewCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: NonEmptyStr
    checks: CaseReviewChecks
    evidence_paths: tuple[NonEmptyStr, ...] = Field(min_length=1)
    finding_ids: tuple[NonEmptyStr, ...]
    rationale: NonEmptyStr


class HelperPlanLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: NonEmptyStr
    section: NonEmptyStr


class HelperReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: NonEmptyStr
    symbol: NonEmptyStr
    declared_kind: NonEmptyStr | None
    observed_signature: NonEmptyStr | None
    observed_async: bool | None
    implementation: Literal["existing", "planned", "unresolved"]
    invocation: Literal["sync", "async", "unknown"]
    target_file: NonEmptyStr
    plan_location: HelperPlanLocation | None
    evidence_paths: tuple[NonEmptyStr, ...] = Field(min_length=1)
    finding_ids: tuple[NonEmptyStr, ...]
    rationale: NonEmptyStr


class PlanReviewAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_refs: tuple[EvidenceArtifactRefV1, ...]
    planning_facts_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[CaseReviewCoverage, ...]
    helpers: tuple[HelperReviewEvidence, ...]
