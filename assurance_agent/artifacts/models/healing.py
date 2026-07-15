"""healing/ artifacts (all must_compat).

- fix-proposal.json: src/schema/fix_proposal.ts + aws-fix-proposal SKILL.md.
  summary.eligible_count is required here (stricter than the TS validator)
  because the healing loop's allocate_on expression reads
  fix_proposal.summary.eligible_count directly.
- *-apply-summary.json: src/schema/apply_summary.ts.
- fixer-safety-check.json: payload written by the TS core healing_state.ts;
  required fields are exactly those the fixer-safety-gate expressions read.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.common import NonEmptyStr

Undetermined = Literal["undetermined"]


class FixProposalSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    eligible_count: int


class FixProposalItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    target: Literal["api", "e2e", "fuzz", "performance"]
    eligible: bool


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    summary: FixProposalSummary
    proposals: list[FixProposalItem]


class ApplySummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    target: Literal["api", "e2e"]
    applied: bool


class SafetyCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    passed: bool
    needs_review: bool
    product_code_modified: bool
    skip_or_xfail_added: bool | Undetermined
    unrelated_tests_modified: bool
    assertion_expected_value_changes_detected: bool
    high_risk_proposal_applied: bool
    bare_return_added: bool | Undetermined | None = None
    change_id: str | None = None
    source_batch_id: str | None = None
    attempt_key: str | None = None
    proposal_sha256: str | None = None
    unrelated_test_files: list[str] | None = None
    assertion_expected_value_changes: list[Any] | None = None
