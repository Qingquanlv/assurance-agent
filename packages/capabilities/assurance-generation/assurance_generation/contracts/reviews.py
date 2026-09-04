"""Plan-review contracts owned by assurance-generation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from assurance_intake.contracts import NonEmptyStr, RiskTier

ReviewDecision = Literal["pass", "needs_fix", "needs_human_review", "reject"]
PublicReviewOutcome = Literal["pass", "needs_fix", "needs_human", "reject"]
PUBLIC_REVIEW_OUTCOMES: tuple[PublicReviewOutcome, ...] = ("pass", "needs_fix", "needs_human", "reject")
FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]

_PASS_DECISIONS = frozenset({"pass"})
_FIX_DECISIONS = frozenset({"needs_fix"})
_HUMAN_DECISIONS = frozenset({"needs_human_review"})
_REJECT_DECISIONS = frozenset({"reject"})


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
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> PublicReviewOutcome:
    return normalize_public_review_outcome(decision, auto_fix_allowed, human_review_required)


_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan"})
_PLAN_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan", "fuzz-plan", "performance-plan"})

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


def _validate_plan_review_routing(
    *,
    decision: ReviewDecision,
    findings: list[Any],
    auto_fix_plan: list[Any],
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> None:
    finding_ids = {
        finding["id"]
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("id"), str)
    }
    for index, finding_id in enumerate(auto_fix_plan):
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError(f"auto_fix_plan[{index}] must be a non-empty finding id")
        if finding_id not in finding_ids:
            raise ValueError(f"auto_fix_plan references unknown finding id: {finding_id}")
    if auto_fix_plan and not auto_fix_allowed:
        raise ValueError("auto_fix_plan requires auto_fix_allowed")
    if decision in _FIX_DECISIONS:
        if auto_fix_allowed:
            if human_review_required:
                raise ValueError("bounded automatic repair cannot also require human review")
            if not auto_fix_plan:
                raise ValueError("bounded automatic repair requires a non-empty auto_fix_plan")
        elif not human_review_required:
            raise ValueError("non-automatic plan repair must require human review")
    if decision == "needs_human_review":
        if auto_fix_allowed or auto_fix_plan or not human_review_required:
            raise ValueError("needs_human_review must route exclusively to human review")
    if decision in _PASS_DECISIONS and human_review_required:
        raise ValueError("a passing plan review cannot require human review")


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["1.0"]
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
        _validate_plan_review_routing(
            decision=self.decision,
            findings=self.findings,
            auto_fix_plan=self.auto_fix_plan or [],
            auto_fix_allowed=bool(self.auto_fix_allowed),
            human_review_required=bool(self.human_review_required),
        )
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

    schema_version: Literal["1.0"]
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
    public_outcome: PublicReviewOutcome | None = None
    rounds_used: int | None = None
    rounds_budget: int | None = None

    @field_validator("findings")
    @classmethod
    def _typed_findings(cls, value: list[Any]) -> list[dict[str, Any]]:
        return _coerce_authoring_findings(value)

    @model_validator(mode="after")
    def _validate_authoring_contract(self, info: ValidationInfo) -> PlanReviewAuthoring:
        _validate_nonblank_finding_ids(self.findings)
        _validate_fully_qualified_capabilities(list(self.required_capabilities))
        _require_exact_capability_leafs(list(self.required_capabilities), info)
        _validate_plan_review_routing(
            decision=self.decision,
            findings=self.findings,
            auto_fix_plan=self.auto_fix_plan,
            auto_fix_allowed=self.auto_fix_allowed,
            human_review_required=self.human_review_required,
        )
        if self.decision in _PASS_DECISIONS:
            outcome: PublicReviewOutcome = "pass"
        else:
            outcome = normalize_public_review_outcome(
                self.decision,
                self.auto_fix_allowed,
                self.human_review_required,
            )
        if self.public_outcome is not None and self.public_outcome != outcome:
            raise ValueError("public_outcome does not match the normalized review decision")
        return self.model_copy(update={"public_outcome": outcome})
