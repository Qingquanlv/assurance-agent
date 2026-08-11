"""review/*.json — reviewer-skill verdicts read by gates (must_compat).

decision / auto_fix_allowed / human_review_required / codegen_readiness /
risk_level / findings are referenced verbatim by workflow-schema.yaml gate
expressions — never rename them. The decision enum covers every value the
packaged schema's review gates compare against (deliberately stricter than
the TS validator, which accepted any non-empty string).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]

# review_type values whose gate (api-plan-review-gate / e2e-plan-review-gate)
# consumes required_capabilities to drive the C7 pre-codegen capability check.
# For these, an omitted / empty list is a reviewer contract error the gate can
# only turn into a dead-end `stop`; fail it here so the reviewer's mandatory
# `aa validate` self-check surfaces it before the gate ever runs.
_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan"})

_CANONICAL_CAPABILITY_PREFIXES = (
    "auth.",
    "accounts.",
    "entities.",
    "auth_matrix.",
    "capabilities.cleanup.",
    "capabilities.domain_factories.",
    "capabilities.adapters.",
)


def _validate_canonical_capability_keys(caps: list[str]) -> None:
    for index, item in enumerate(caps):
        key = item.strip()
        if not key or any(not part for part in key.split(".")):
            raise ValueError(f"required_capabilities[{index}] must be a non-empty leaf key string")
        if not key.startswith(_CANONICAL_CAPABILITY_PREFIXES):
            raise ValueError(
                f"required_capabilities[{index}] must be a canonical C4 leaf key; "
                "use auth.*, accounts.*, entities.*, auth_matrix.*, or capabilities.*"
            )


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
        _validate_canonical_capability_keys(caps)
        return self


class PlanReview(Review):
    """api/e2e plan review：继承 Review 的兼容字段，并强制校验跨技能消费契约。"""

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
        if self.review_type not in _CAPABILITY_GATED_REVIEW_TYPES:
            raise ValueError("plan review review_type must be api-plan or e2e-plan")
        for index, finding in enumerate(self.findings):
            if not isinstance(finding, dict) or not isinstance(finding.get("id"), str):
                raise ValueError(f"findings[{index}].id must be a non-empty string")
            if not finding["id"].strip():
                raise ValueError(f"findings[{index}].id must be a non-empty string")
        return self


class PlanReviewAuthoring(BaseModel):
    """Agent-authored fields required by plan gates, codegen, and fixer."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "prompt_notes": [
                "each findings item requires id",
                "auto_fix_plan items must reference an existing findings id",
                "required_capabilities must be a non-empty list of fully qualified C4 leaf keys",
                "use auth.*, accounts.*, entities.*, capabilities.domain_factories.*, "
                "capabilities.adapters.*, or capabilities.cleanup.* exactly as rooted in L1",
            ]
        },
    )

    schema_version: NonEmptyStr
    review_type: Literal["api-plan", "e2e-plan"]
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

    @model_validator(mode="after")
    def _require_canonical_capability_keys(self) -> "PlanReviewAuthoring":
        _validate_canonical_capability_keys(self.required_capabilities)
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
