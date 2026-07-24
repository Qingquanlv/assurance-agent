"""review/*.json — reviewer-skill verdicts read by gates (must_compat).

decision / auto_fix_allowed / human_review_required / codegen_readiness /
risk_level / findings are referenced verbatim by workflow-schema.yaml gate
expressions — never rename them. The decision enum covers every value the
packaged schema's review gates compare against (deliberately stricter than
the TS validator, which accepted any non-empty string).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

ReviewDecision = Literal["pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"]

# review_type values whose gate (api-plan-review-gate / e2e-plan-review-gate)
# consumes required_capabilities to drive the C7 pre-codegen capability check.
# For these, an omitted / empty list is a reviewer contract error the gate can
# only turn into a dead-end `stop`; fail it here so the reviewer's mandatory
# `aa validate` self-check surfaces it before the gate ever runs.
_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan"})


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    review_type: str | None = None
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
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"required_capabilities[{index}] must be a non-empty leaf key string"
                )
        return self
