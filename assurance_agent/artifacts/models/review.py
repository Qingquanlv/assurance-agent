"""review/*.json — reviewer-skill verdicts read by gates (must_compat).

decision / auto_fix_allowed / human_review_required / codegen_readiness /
risk_level / findings are referenced verbatim by workflow-schema.yaml gate
expressions — never rename them. The decision enum covers every value the
packaged schema's review gates compare against (deliberately stricter than
the TS validator, which accepted any non-empty string).
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.common import NonEmptyStr

ReviewDecision = Literal[
    "pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"
]


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    auto_fix_allowed: bool | None = None
    human_review_required: bool | None = None
    codegen_readiness: Literal["ready", "ready_with_warnings", "not_ready"] | None = None
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
