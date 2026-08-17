"""review/*.json — reviewer-skill verdicts read by gates (must_compat).

decision / auto_fix_allowed / human_review_required / codegen_readiness /
risk_level / findings are referenced verbatim by workflow-schema.yaml gate
expressions — never rename them. The decision enum covers every value the
packaged schema's review gates compare against (deliberately stricter than
the TS validator, which accepted any non-empty string).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]

# review_type values whose gate (api-plan-review-gate / e2e-plan-review-gate)
# consumes required_capabilities to drive the C7 pre-codegen capability check.
# For these, an omitted / empty list is a reviewer contract error the gate can
# only turn into a dead-end `stop`; fail it here so the reviewer's mandatory
# `aa validate` self-check surfaces it before the gate ever runs.
_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan"})
_PLAN_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan", "fuzz-plan", "performance-plan"})
_HUMAN_ONLY_PLAN_REVIEW_TYPES = frozenset({"fuzz-plan", "performance-plan"})

_L1_CAPABILITY_ROOTS = (
    "auth.",
    "accounts.",
    "entities.",
    "auth_matrix.",
    "capabilities.cleanup.",
    "capabilities.domain_factories.",
    "capabilities.adapters.",
)


def _is_fully_qualified_capability_key(key: str) -> bool:
    if not key or key != key.strip() or "." not in key:
        return False
    for root in _L1_CAPABILITY_ROOTS:
        if not key.startswith(root):
            continue
        remainder = key[len(root) :]
        return bool(remainder) and not remainder.endswith(".")
    return False


def _validate_fully_qualified_capabilities(caps: list[str] | None) -> None:
    if not isinstance(caps, list) or len(caps) == 0:
        raise ValueError("required_capabilities must be a non-empty list of fully qualified L1 leaf keys")
    for index, item in enumerate(caps):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"required_capabilities[{index}] must be a non-empty leaf key string")
        if not _is_fully_qualified_capability_key(item):
            raise ValueError(f"required_capabilities[{index}] must be a canonical C4 leaf key")


FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]


class ReviewFindingLocator(BaseModel):
    """Stable pointer into the reviewed artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact: NonEmptyStr
    case_id: str | None = None
    key: str | None = None


class ReviewFinding(BaseModel):
    """Typed review finding; locator makes author re-entry mechanically checkable."""

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
        coerced.append(ReviewFinding.model_validate(finding).model_dump())
    return coerced


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    review_type: str | None = None
    change_id: str | None = None
    auto_fix_allowed: bool | None = None
    human_review_required: bool | None = None
    codegen_readiness: Literal["ready", "ready_with_warnings", "not_ready"] | None = None
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    required_capabilities: list[str] | None = None
    # Set false by the fuzz-/performance-plan reviewers when the layer was
    # selected in the proposal but has zero applicable cases (empty scope).
    # The fuzz/performance plan-review gates read this to route a graceful
    # `skip` (branch ends, codegen skipped) instead of a dead-end `reject`.
    layer_applicable: bool | None = None
    auto_fix_plan: list[Any] | None = None
    next_action: str | None = None

    @model_validator(mode="after")
    def _require_capabilities_for_plan_reviews(self) -> "Review":
        if self.review_type not in _CAPABILITY_GATED_REVIEW_TYPES:
            return self
        caps = self.required_capabilities
        if not isinstance(caps, list) or len(caps) == 0:
            raise ValueError(
                f"required_capabilities must be a non-empty list for review_type "
                f"'{self.review_type}' (drives the pre-codegen capability gate)"
            )
        for index, item in enumerate(caps):
            if not isinstance(item, str):
                raise ValueError(f"required_capabilities[{index}] must be a non-empty leaf key string")
        _validate_fully_qualified_capabilities(caps)
        return self


class PlanReview(Review):
    """Strong cross-skill contract for API, E2E, Fuzz, and Performance plans."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "prompt_notes": [
                "required_capabilities must be a non-empty list of fully qualified C4 leaf keys",
                "use auth.*, accounts.*, entities.*, capabilities.domain_factories.*, "
                "capabilities.adapters.*, or capabilities.cleanup.* exactly as rooted in L1",
            ]
        },
    )

    @model_validator(mode="after")
    def _require_cross_skill_fields(self) -> "PlanReview":
        required = (
            "review_type",
            "change_id",
            "auto_fix_allowed",
            "human_review_required",
            "codegen_readiness",
            "risk_level",
            "required_capabilities",
            "auto_fix_plan",
            "next_action",
        )
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError("plan review missing cross-skill fields: " + ", ".join(missing))
        if self.review_type not in _PLAN_REVIEW_TYPES:
            raise ValueError(f"unsupported plan review_type {self.review_type!r}")
        _validate_nonblank_finding_ids(self.findings)
        _validate_fully_qualified_capabilities(self.required_capabilities)
        if self.review_type in _HUMAN_ONLY_PLAN_REVIEW_TYPES:
            if self.auto_fix_allowed or self.auto_fix_plan:
                raise ValueError("human-only plan review cannot authorize automatic fixes")
        return self


class PlanReviewAuthoring(BaseModel):
    """Agent-authored fields required by plan gates, codegen, and fixer."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "prompt_notes": [
                "each findings item requires id, severity, category, message, and locator",
                "locator.artifact is a change- or repo-relative path; locator.case_id / locator.key are optional",
                "auto_fix_plan items must reference an existing findings id",
                "required_capabilities must be a non-empty list of fully qualified C4 leaf keys",
                "use auth.*, accounts.*, entities.*, capabilities.domain_factories.*, "
                "capabilities.adapters.*, or capabilities.cleanup.* exactly as rooted in L1",
            ]
        },
    )

    schema_version: NonEmptyStr
    review_type: Literal["api-plan", "e2e-plan", "fuzz-plan", "performance-plan"]
    change_id: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    auto_fix_plan: list[Any]
    next_action: NonEmptyStr
    auto_fix_allowed: bool
    human_review_required: bool
    codegen_readiness: Literal["ready", "ready_with_warnings", "not_ready"]
    risk_level: Literal["low", "medium", "high", "critical"]
    required_capabilities: list[NonEmptyStr]

    @field_validator("findings")
    @classmethod
    def _typed_findings(cls, value: list[Any]) -> list[dict[str, Any]]:
        return _coerce_authoring_findings(value)

    @model_validator(mode="after")
    def _validate_authoring_contract(self) -> "PlanReviewAuthoring":
        _validate_nonblank_finding_ids(self.findings)
        _validate_fully_qualified_capabilities(list(self.required_capabilities))
        if self.decision == "approved":
            raise ValueError(
                "new plan reviews must use decision 'pass'; 'approved' is read-only compatibility"
            )
        if self.review_type in _HUMAN_ONLY_PLAN_REVIEW_TYPES:
            if self.auto_fix_allowed:
                raise ValueError("human-only plan review cannot authorize automatic fixes")
            if self.auto_fix_plan:
                raise ValueError("auto_fix_plan must be empty for human-only plan review")
        return self


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
    def _require_product_source_evidence(self) -> "CaseSourceVerification":
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
    def _internally_consistent(self) -> "CaseMinimumCoverageReview":
        if self.total_required != self.covered + self.skipped_by_scope:
            raise ValueError("total_required must equal covered + skipped_by_scope")
        if len(self.missing) != len(set(self.missing)):
            raise ValueError("minimum_coverage.missing must not contain duplicates")
        if len(self.missing) != self.skipped_by_scope:
            raise ValueError("minimum_coverage.missing must list every skipped_by_scope key")
        return self


class CaseReviewAuthoring(BaseModel):
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
