"""Case-review contracts owned by assurance-intake."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_intake.contracts.common import NonEmptyStr

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]
PublicReviewOutcome = Literal["pass", "needs_fix", "needs_human", "reject"]
PUBLIC_REVIEW_OUTCOMES: tuple[PublicReviewOutcome, ...] = ("pass", "needs_fix", "needs_human", "reject")
FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]

_PASS_DECISIONS = frozenset({"pass", "approved"})
_FIX_DECISIONS = frozenset({"needs_fix", "changes_requested"})
_HUMAN_DECISIONS = frozenset({"needs_human_review"})
_REJECT_DECISIONS = frozenset({"reject"})


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
    findings: list[CaseReviewFindingV1]
    auto_fix_plan: list[Any]
    next_action: NonEmptyStr
    auto_fix_allowed: bool
    human_review_required: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    minimum_coverage: CaseMinimumCoverageReview
    source_verification: CaseSourceVerification
    public_outcome: PublicReviewOutcome | None = None
    rounds_used: int | None = Field(default=None, ge=0)
    rounds_budget: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _normalize_public_outcome(self) -> CaseReviewResultV1:
        outcome = normalize_public_review_outcome(
            self.decision,
            self.auto_fix_allowed,
            self.human_review_required,
        )
        if self.public_outcome is not None and self.public_outcome != outcome:
            raise ValueError("public_outcome does not match the normalized review decision")
        if self.rounds_used is not None and self.rounds_budget is not None:
            if self.rounds_used > self.rounds_budget:
                raise ValueError("rounds_used cannot exceed rounds_budget")
        return self.model_copy(update={"public_outcome": outcome})


def normalize_public_review_outcome(
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> PublicReviewOutcome:
    if decision in _PASS_DECISIONS:
        if auto_fix_allowed or human_review_required:
            raise ValueError("pass review cannot request auto-fix or human review")
        return "pass"
    if decision in _FIX_DECISIONS:
        if auto_fix_allowed and not human_review_required:
            return "needs_fix"
        if human_review_required and not auto_fix_allowed:
            return "needs_human"
        raise ValueError("needs_fix review must be either auto-fixable or human-required")
    if decision in _HUMAN_DECISIONS:
        if auto_fix_allowed or not human_review_required:
            raise ValueError("needs_human_review must require human review and forbid auto-fix")
        return "needs_human"
    if decision in _REJECT_DECISIONS:
        if auto_fix_allowed or human_review_required:
            raise ValueError("reject review cannot request auto-fix or human review")
        return "reject"
    raise ValueError(f"unsupported review decision: {decision}")


def public_review_outcome(
    decision: str | CaseReviewResultV1,
    auto_fix_allowed: bool | None = None,
    human_review_required: bool | None = None,
) -> PublicReviewOutcome:
    public_outcome = getattr(decision, "public_outcome", None)
    raw_decision = getattr(decision, "decision", None)
    if public_outcome is not None and raw_decision is not None:
        return public_outcome  # type: ignore[return-value]
    if not isinstance(decision, str):
        raise TypeError("decision must be a review decision string or CaseReviewResultV1")
    if auto_fix_allowed is None or human_review_required is None:
        raise TypeError("auto_fix_allowed and human_review_required are required")
    return normalize_public_review_outcome(decision, auto_fix_allowed, human_review_required)
