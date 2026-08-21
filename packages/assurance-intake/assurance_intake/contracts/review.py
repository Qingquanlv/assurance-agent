"""Case-review contracts owned by assurance-intake."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_intake.contracts.common import NonEmptyStr

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]
FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]


class ReviewFindingLocator(BaseModel):
    """Stable pointer into the reviewed artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact: NonEmptyStr
    case_id: str | None = None
    key: str | None = None


class CaseReviewFindingV1(BaseModel):
    """Typed case-review finding; locator makes author re-entry mechanically checkable."""

    model_config = ConfigDict(extra="allow")

    id: NonEmptyStr
    severity: FindingSeverity
    category: NonEmptyStr
    message: NonEmptyStr
    locator: ReviewFindingLocator


def _validate_nonblank_finding_ids(findings: list[Any]) -> None:
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str):
            raise ValueError(f"findings[{index}].id must be a non-empty string")
        if not finding["id"].strip():
            raise ValueError(f"findings[{index}].id must be a non-empty string")


def _coerce_authoring_findings(findings: list[Any]) -> list[dict[str, Any]]:
    _validate_nonblank_finding_ids(findings)
    coerced: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("findings items must be objects")
        coerced.append(CaseReviewFindingV1.model_validate(finding).model_dump())
    return coerced


class CaseSourceClaim(BaseModel):
    """One independently checked product claim and its source-code evidence."""

    model_config = ConfigDict(extra="forbid")

    claim: NonEmptyStr
    evidence_files: list[NonEmptyStr] = Field(min_length=1)


class CaseSourceVerification(BaseModel):
    """Proof that case review inspected product source independently of the author."""

    model_config = ConfigDict(extra="forbid")

    independent: Literal[True]
    reviewed_source_files: list[NonEmptyStr] = Field(min_length=1)
    verified_claims: list[CaseSourceClaim] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_product_source_evidence(self) -> CaseSourceVerification:
        source_files = set(self.reviewed_source_files)
        disallowed_prefixes = (
            "qa/",
            ".aa/",
            ".opencode/",
            "docs/",
            "requirements/",
            "tests/",
        )
        for path in source_files:
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("reviewed_source_files must be project-relative paths")
            if path.startswith(disallowed_prefixes):
                raise ValueError(
                    "reviewed_source_files must name product source, not QA artifacts, "
                    "requirements, docs, or tests"
                )
        for index, claim in enumerate(self.verified_claims):
            unknown = set(claim.evidence_files) - source_files
            if unknown:
                raise ValueError(
                    f"verified_claims[{index}].evidence_files must be listed in "
                    f"reviewed_source_files: {sorted(unknown)}"
                )
        return self


class CaseMinimumCoverageReview(BaseModel):
    """Reviewer projection of the frozen MRC matrix; runtime verifies every field."""

    model_config = ConfigDict(extra="forbid")

    total_required: int = Field(ge=0)
    covered: int = Field(ge=0)
    skipped_by_scope: int = Field(ge=0)
    missing: list[NonEmptyStr]

    @model_validator(mode="after")
    def _internally_consistent(self) -> CaseMinimumCoverageReview:
        if self.total_required != self.covered + self.skipped_by_scope:
            raise ValueError("total_required must equal covered + skipped_by_scope")
        if len(self.missing) != len(set(self.missing)):
            raise ValueError("minimum_coverage.missing must not contain duplicates")
        if len(self.missing) != self.skipped_by_scope:
            raise ValueError("minimum_coverage.missing must list every skipped_by_scope key")
        return self


class CaseReviewResultV1(BaseModel):
    """Agent-authored case review, including independent SUT-source evidence."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "prompt_notes": [
                "source_verification is mandatory and must come from an independent read of product source",
                "source_verification.independent must be true; source_verification.reviewed_source_files "
                "must list product source; source_verification.verified_claims must be non-empty; each "
                "verified_claims[].claim and verified_claims[].evidence_files must be non-empty",
                "QA artifacts, requirements, docs, and tests do not count as product source evidence",
                "minimum_coverage is a projection of trace/minimum-coverage-matrix.yaml: count only "
                "required rows; covered counts status=covered; skipped_by_scope and missing must list "
                "every required skipped row in matrix order",
                "each findings item requires id, severity, category, message, and locator",
            ]
        },
    )

    schema_version: NonEmptyStr
    review_type: Literal["case"]
    change_id: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    auto_fix_plan: list[Any]
    next_action: NonEmptyStr
    auto_fix_allowed: bool
    human_review_required: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    minimum_coverage: CaseMinimumCoverageReview
    source_verification: CaseSourceVerification

    @field_validator("findings")
    @classmethod
    def _typed_findings(cls, value: list[Any]) -> list[dict[str, Any]]:
        return _coerce_authoring_findings(value)
