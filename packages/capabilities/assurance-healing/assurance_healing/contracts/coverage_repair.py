"""Coverage-repair brief, status, baseline, and safety contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from assurance_healing.contracts.wire import FrozenContract, validate_repo_path
from assurance_intake.contracts import NonEmptyStr

RepairableGapKind = Literal[
    "uncovered_required_case",
    "stale_required_case",
    "constraint_without_property",
    "matrix_cell_unasserted",
]
CoverageRepairStatusValue = Literal[
    "exhausted", "failed", "in_progress", "needs_review", "not_eligible", "repaired"
]
HealingRepairOutcome = Literal["exhausted", "failed", "needs_review", "not_eligible", "repaired"]
HEALING_REPAIR_OUTCOMES: tuple[HealingRepairOutcome, ...] = (
    "exhausted",
    "failed",
    "needs_review",
    "not_eligible",
    "repaired",
)
CoverageGapKind = Literal[
    "uncovered_required_case",
    "stale_required_case",
    "constraint_without_property",
    "matrix_cell_unasserted",
    "unmapped_test_cluster",
    "obligation_case_missing",
    "obligation_mapping_missing",
    "obligation_observation_missing",
]
MetricsSufficiencyVerdict = Literal["pass", "needs_human", "reject", "stop", "skipped"]


class RepairLocator(FrozenContract):
    case_id: NonEmptyStr | None = None
    constraint_key: NonEmptyStr | None = None
    cell: NonEmptyStr | None = None
    cluster_key: NonEmptyStr | None = None
    mrc_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _at_least_one_key(self) -> Self:
        if not any((self.case_id, self.constraint_key, self.cell, self.cluster_key, self.mrc_id)):
            raise ValueError(
                "locator requires at least one of case_id, constraint_key, cell, cluster_key, mrc_id"
            )
        return self


class RepairShortboard(FrozenContract):
    metric: NonEmptyStr
    detail: str = ""


class RepairItem(FrozenContract):
    kind: RepairableGapKind
    locator: RepairLocator
    metric: NonEmptyStr
    hint: str = ""


class DeferredItem(FrozenContract):
    kind: CoverageGapKind
    locator: RepairLocator
    reason: Literal[
        "declaration_layer",
        "unmapped_cluster",
        "not_repairable_metric",
        "not_serving_shortboard",
        "no_test_scope",
        "obligation_scope",
    ]


class CoverageRepairBrief(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    batch_id: NonEmptyStr | None = None
    probe_verdict: MetricsSufficiencyVerdict
    eligible: bool
    allowed_test_files: tuple[NonEmptyStr, ...] = ()
    shortboards: tuple[RepairShortboard, ...] = ()
    repair_items: tuple[RepairItem, ...] = ()
    deferred_to_intake: tuple[DeferredItem, ...] = ()
    computed_at: datetime | None = None

    @model_validator(mode="after")
    def _eligible_requires_a_batch_and_items(self) -> Self:
        if self.eligible and (self.batch_id is None or not self.repair_items or not self.allowed_test_files):
            raise ValueError(
                "eligible brief requires a batch_id, at least one repair item, and allowed_test_files"
            )
        if len(self.allowed_test_files) != len(set(self.allowed_test_files)):
            raise ValueError("allowed_test_files must not contain duplicates")
        for path in self.allowed_test_files:
            validate_repo_path(path)
        return self


class CoverageRepairStatus(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    status: CoverageRepairStatusValue
    attempts_used: int = Field(ge=0)
    last_batch_id: NonEmptyStr | None = None
    deferred_to_intake: tuple[DeferredItem, ...] = ()


class CoverageRepairApplySummary(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    attempt: int = Field(ge=1)
    attempt_token: NonEmptyStr
    applied: bool
    files_modified: tuple[str, ...] = ()
    addressed_items: tuple[str, ...] = ()
    notes: str = ""


class CoverageRepairBaseline(FrozenContract):
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


class CoverageRepairSafetyCheck(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    attempt: int = Field(ge=1)
    passed: bool
    needs_review: bool = False
    test_files_changed: tuple[str, ...] = ()
    product_code_modified: bool = False
    product_files_changed: tuple[str, ...] = ()
    declaration_files_modified: bool = False
    declaration_files_changed: tuple[str, ...] = ()
    skip_or_xfail_added: bool = False
    unbriefed_files_modified: tuple[str, ...] = ()
    summary_applied: bool = False
    summary_files_modified: tuple[str, ...] = ()
    summary_mismatch: bool = False
    stale_summary: bool = False
