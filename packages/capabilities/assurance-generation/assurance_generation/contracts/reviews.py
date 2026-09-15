"""Plan-review contracts owned by assurance-generation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from assurance_intake.contracts import NonEmptyStr, RiskTier

ReviewDecision = Literal["pass", "needs_fix", "needs_human_review", "reject"]
PlanReviewRoute = Literal["codegen", "auto_fix", "human", "reject"]
PublicReviewOutcome = Literal["pass", "needs_fix", "needs_human", "reject"]
PUBLIC_REVIEW_OUTCOMES: tuple[PublicReviewOutcome, ...] = ("pass", "needs_fix", "needs_human", "reject")
FindingSeverity = Literal["low", "medium", "high", "critical", "blocking"]

_ROUTE_PUBLIC_OUTCOME: dict[str, PublicReviewOutcome] = {
    "codegen": "pass",
    "auto_fix": "needs_fix",
    "human": "needs_human",
    "reject": "reject",
}
_REMOVED_ROUTING_FIELDS = (
    "decision",
    "auto_fix_allowed",
    "human_review_required",
    "auto_fix_plan",
    "codegen_readiness",
)


def normalize_public_review_outcome(route: str) -> PublicReviewOutcome:
    try:
        return _ROUTE_PUBLIC_OUTCOME[route]
    except KeyError as error:
        raise ValueError(f"unsupported plan review route: {route}") from error


def public_review_outcome(route: str) -> PublicReviewOutcome:
    return normalize_public_review_outcome(route)


_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-codegen", "e2e-codegen"})
_CODEGEN_REVIEW_TYPES = frozenset({"api-codegen", "e2e-codegen", "fuzz-codegen", "performance-codegen"})

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


def _reject_removed_routing_fields(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    present = [name for name in _REMOVED_ROUTING_FIELDS if name in data]
    if present:
        raise ValueError("plan review routing fields are route and finding_ids; remove " + ", ".join(present))
    return data


def _validate_plan_review_routing(
    *,
    route: PlanReviewRoute,
    findings: list[Any],
    finding_ids: list[Any],
) -> None:
    reported = {
        finding["id"]
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("id"), str)
    }
    if len(reported) != len(findings):
        raise ValueError("plan review finding IDs must be unique")
    ids: list[str] = []
    for index, finding_id in enumerate(finding_ids):
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError(f"finding_ids[{index}] must be a non-empty finding id")
        if finding_id not in reported:
            raise ValueError(f"finding_ids references unknown finding id: {finding_id}")
        ids.append(finding_id)
    if len(ids) != len(set(ids)):
        raise ValueError("finding_ids must be unique")
    if route == "auto_fix":
        if not ids:
            raise ValueError("auto_fix requires a non-empty finding_ids list")
        if set(ids) != reported:
            raise ValueError("finding_ids must include every finding exactly once")
        return
    if route in {"codegen", "human", "reject"}:
        if ids:
            raise ValueError(f"{route} route requires an empty finding_ids list")
        return
    raise ValueError(f"unsupported plan review route: {route}")


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["1.0"]
    findings: list[Any]
    review_type: str | None = None
    change_id: str | None = None
    route: PlanReviewRoute | None = None
    finding_ids: list[Any] | None = None
    risk_level: RiskTier | None = None
    required_capabilities: list[str] | None = None
    layer_applicable: bool | None = None
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

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_routing(cls, data: Any) -> Any:
        return _reject_removed_routing_fields(data)

    @model_validator(mode="after")
    def _require_cross_skill_fields(self, info: ValidationInfo) -> PlanReview:
        required = (
            "review_type",
            "change_id",
            "route",
            "finding_ids",
            "risk_level",
            "required_capabilities",
            "next_action",
        )
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError("plan review missing cross-skill fields: " + ", ".join(missing))
        if self.review_type not in _CODEGEN_REVIEW_TYPES:
            raise ValueError(f"unsupported codegen review_type {self.review_type!r}")
        _validate_nonblank_finding_ids(self.findings)
        _validate_fully_qualified_capabilities(self.required_capabilities)
        _require_exact_capability_leafs(self.required_capabilities, info)
        _validate_plan_review_routing(
            route=self.route,  # type: ignore[arg-type]
            findings=self.findings,
            finding_ids=self.finding_ids or [],
        )
        return self


class PlanReviewAuthoring(BaseModel):
    """Agent-authored fields required by plan gates, codegen, and fixer."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "prompt_notes": [
                "routing fields are only route and finding_ids",
                "route is codegen, auto_fix, human, or reject",
                "each findings item requires id, severity, category, message, and locator",
                "locator.artifact is a change- or repo-relative path; locator.case_id / locator.key are optional",
                "finding_ids items must reference an existing findings id",
                "required_capabilities must be a non-empty list of fully qualified C4 leaf keys",
                "use auth.*, accounts.*, entities.*, capabilities.domain_factories.*, "
                "capabilities.adapters.*, or capabilities.cleanup.* exactly as rooted in L1",
            ]
        },
    )

    schema_version: Literal["1.0"]
    review_type: Literal["api-codegen", "e2e-codegen", "fuzz-codegen", "performance-codegen"]
    change_id: NonEmptyStr
    route: PlanReviewRoute
    findings: list[Any]
    finding_ids: list[Any]
    next_action: NonEmptyStr
    risk_level: RiskTier
    required_capabilities: list[NonEmptyStr]
    public_outcome: PublicReviewOutcome | None = None
    rounds_used: int | None = None
    rounds_budget: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_routing(cls, data: Any) -> Any:
        return _reject_removed_routing_fields(data)

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
            route=self.route,
            findings=self.findings,
            finding_ids=self.finding_ids,
        )
        outcome = normalize_public_review_outcome(self.route)
        if self.public_outcome is not None and self.public_outcome != outcome:
            raise ValueError("public_outcome does not match the normalized review route")
        return self.model_copy(update={"public_outcome": outcome})
