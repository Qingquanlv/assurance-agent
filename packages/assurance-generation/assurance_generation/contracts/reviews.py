"""Plan-review contracts owned by assurance-generation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from assurance_intake.contracts import NonEmptyStr, RiskTier

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]
FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]

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


def _require_exact_capability_leafs(caps: list[str] | None, info: ValidationInfo) -> None:
    context = info.context or {}
    leafs = context.get("capability_leafs")
    if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
    if not caps:
        return
    for item in caps:
        if item not in leafs:
            raise ValueError(f"unknown capability leaf: {item}")


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
    risk_level: RiskTier | None = None
    required_capabilities: list[str] | None = None
    layer_applicable: bool | None = None
    auto_fix_plan: list[Any] | None = None
    next_action: str | None = None

    @model_validator(mode="after")
    def _require_capabilities_for_plan_reviews(self, info: ValidationInfo) -> Review:
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
        _require_exact_capability_leafs(caps, info)
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
    def _require_cross_skill_fields(self, info: ValidationInfo) -> PlanReview:
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
        _require_exact_capability_leafs(self.required_capabilities, info)
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
    risk_level: RiskTier
    required_capabilities: list[NonEmptyStr]

    @field_validator("findings")
    @classmethod
    def _typed_findings(cls, value: list[Any]) -> list[dict[str, Any]]:
        return _coerce_authoring_findings(value)

    @model_validator(mode="after")
    def _validate_authoring_contract(self, info: ValidationInfo) -> PlanReviewAuthoring:
        _validate_nonblank_finding_ids(self.findings)
        _validate_fully_qualified_capabilities(list(self.required_capabilities))
        _require_exact_capability_leafs(list(self.required_capabilities), info)
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
