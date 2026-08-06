"""coverage-repair/* artifacts (all must_compat).

Typed carriers for the coverage-repair inner loop (design
``docs/superpowers/specs/2026-08-06-coverage-repair-inner-loop-design.md``):

- ``brief.json`` — probe → entry / loop gates
- ``status.json`` — allocate / record-status → loop gate
- ``safety-check.json`` — compute-safety → safety gate
- ``apply-summary.json`` — skill declared output → compute-safety cross-check
- ``entry-baseline.json`` — allocate tree freeze → compute-safety mechanical diff

All five steer gate routing or the safety verdict that feeds a gate, so they
register as ``must_compat``: a document this release cannot fully validate must
be refused rather than read partially and routed as a pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.coverage_gaps import CoverageGapKind, CoverageGapLocator
from assurance_agent.artifacts.models.metrics import MetricKey, MetricShortboard

_FROZEN = ConfigDict(frozen=True, extra="forbid")

COVERAGE_REPAIR_BRIEF_REL = "coverage-repair/brief.json"
COVERAGE_REPAIR_STATUS_REL = "coverage-repair/status.json"
COVERAGE_REPAIR_SAFETY_REL = "coverage-repair/safety-check.json"
COVERAGE_REPAIR_APPLY_SUMMARY_REL = "coverage-repair/apply-summary.json"
COVERAGE_REPAIR_BASELINE_REL = "coverage-repair/entry-baseline.json"

RepairableGapKind = Literal[
    "uncovered_required_case",
    "stale_required_case",
    "constraint_without_property",
    "matrix_cell_unasserted",
]
CoverageRepairStatusValue = Literal["repaired", "exhausted", "not_eligible", "failed", "in_progress"]

# Re-declared locally: ``MetricsSufficiencyVerdict`` lives in the evidence layer
# above artifacts. Importing it here would invert the layer contract. Keep the
# literals identical; tests assert ``get_args()`` equality.
MetricsSufficiencyVerdict = Literal["pass", "needs_human", "reject", "stop", "skipped"]


class RepairItem(BaseModel):
    """One gap the coverage-repair skill may attempt to close."""

    model_config = _FROZEN

    kind: RepairableGapKind
    locator: CoverageGapLocator
    metric: MetricKey
    hint: str = ""


class DeferredItem(BaseModel):
    """A gap deferred out of the repair loop (declaration layer / unmapped / etc.)."""

    model_config = _FROZEN

    kind: CoverageGapKind
    locator: CoverageGapLocator
    reason: Literal["declaration_layer", "unmapped_cluster", "not_repairable_metric"]


class CoverageRepairBrief(BaseModel):
    """Probe output: eligibility + repair batch for the entry / loop gates."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    batch_id: NonEmptyStr | None = None
    probe_verdict: MetricsSufficiencyVerdict
    eligible: bool
    shortboards: tuple[MetricShortboard, ...] = ()
    repair_items: tuple[RepairItem, ...] = ()
    deferred_to_intake: tuple[DeferredItem, ...] = ()
    computed_at: datetime | None = None

    @model_validator(mode="after")
    def _eligible_requires_a_batch_and_items(self) -> Self:
        if self.eligible and (self.batch_id is None or not self.repair_items):
            raise ValueError("eligible brief requires a batch_id and at least one repair item")
        return self


class CoverageRepairStatus(BaseModel):
    """Loop budget / outcome ledger read by the coverage-repair loop gate."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    status: CoverageRepairStatusValue
    attempts_used: int = Field(ge=0)
    last_batch_id: NonEmptyStr | None = None
    deferred_to_intake: tuple[DeferredItem, ...] = ()


class CoverageRepairApplySummary(BaseModel):
    """The repair skill's declared output. Self-reported: a cross-check, never evidence.

    ``change_id`` / ``attempt`` / ``attempt_token`` bind the summary to one specific
    attempt. Without them a second attempt could satisfy the declared-output check
    with the *first* attempt's file (design v4-5).
    """

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    attempt: int = Field(ge=1)
    attempt_token: NonEmptyStr
    applied: bool
    files_modified: tuple[str, ...] = ()
    addressed_items: tuple[str, ...] = ()
    notes: str = ""


class CoverageRepairBaseline(BaseModel):
    """Tree hashes frozen by ``allocate`` *before* the skill runs (design v4-2)."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    attempt: int = Field(ge=1)
    attempt_token: NonEmptyStr
    test_tree_sha256: NonEmptyStr
    test_files_sha256: dict[str, str]
    product_tree_sha256: NonEmptyStr
    product_files_sha256: dict[str, str]
    declaration_tree_sha256: NonEmptyStr
    declaration_files_sha256: dict[str, str]


class CoverageRepairSafetyCheck(BaseModel):
    """Mechanical facts from ``diff_trees`` plus summary_* cross-check fields.

    Everything above the cross-check divider is the sole input to ``passed`` /
    ``needs_review``. ``summary_*`` records what the skill claimed; a stale
    summary maps to ``needs_review``, never weakens ``passed`` or mechanical fields.
    """

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    attempt: int = Field(ge=1)
    passed: bool
    needs_review: bool = False
    # --- mechanical facts: diff_trees(baseline, current). The only judgment inputs. ---
    test_files_changed: tuple[str, ...] = ()
    product_code_modified: bool = False
    product_files_changed: tuple[str, ...] = ()
    declaration_files_modified: bool = False
    declaration_files_changed: tuple[str, ...] = ()
    skip_or_xfail_added: bool = False
    unbriefed_files_modified: tuple[str, ...] = ()
    # --- cross-check: what the skill claimed, and whether it matches ---
    summary_applied: bool = False
    summary_files_modified: tuple[str, ...] = ()
    summary_mismatch: bool = False
    stale_summary: bool = False


__all__ = [
    "COVERAGE_REPAIR_APPLY_SUMMARY_REL",
    "COVERAGE_REPAIR_BASELINE_REL",
    "COVERAGE_REPAIR_BRIEF_REL",
    "COVERAGE_REPAIR_SAFETY_REL",
    "COVERAGE_REPAIR_STATUS_REL",
    "CoverageRepairApplySummary",
    "CoverageRepairBaseline",
    "CoverageRepairBrief",
    "CoverageRepairSafetyCheck",
    "CoverageRepairStatus",
    "CoverageRepairStatusValue",
    "DeferredItem",
    "MetricsSufficiencyVerdict",
    "RepairItem",
    "RepairableGapKind",
]
