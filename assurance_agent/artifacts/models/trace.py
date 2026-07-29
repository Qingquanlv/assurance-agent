"""inspect/trace-projection.json — fact-only case↔execution↔issue projection.

Projection carries no policy judgment and no wall-clock freshness; consumers
apply ``evaluate_sufficiency(projection, policy, *, as_of)`` at the use site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True, extra="forbid")

TraceGapCode = Literal[
    "result_missing",
    "result_corrupt",
    "batch_id_unparseable",
    "manifest_missing",
    "case_unreadable",
    "failure_analysis_missing",
    "issues_snapshot_missing",
    "problems_snapshot_missing",
    "mapped_test_missing_from_tree",
    "tests_tree_digest_mismatch",
    "result_identity_mismatch",
    "problem_alias_invalid",
]

TraceIntegrity = Literal["complete", "degraded", "incomplete"]


class TraceExecution(BaseModel):
    model_config = _FROZEN

    batch_id: str
    target: Literal["api", "e2e", "fuzz", "performance"]
    status: Literal["passed", "failed", "skipped"]
    ts: datetime
    ts_source: Literal["executed_at", "batch_id_legacy_utc"]


class TraceFailure(BaseModel):
    model_config = _FROZEN

    category: str
    severity: str


class TraceGap(BaseModel):
    model_config = _FROZEN

    code: TraceGapCode
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class TraceTestRef(BaseModel):
    model_config = _FROZEN

    file: str
    function: str


class UnmappedTest(BaseModel):
    model_config = _FROZEN

    file: str
    test_name: str


class TraceRow(BaseModel):
    model_config = _FROZEN

    case_id: str
    module: str
    case_type: Literal["API", "E2E", "Fuzz", "Performance"]
    automation_required: bool
    assertions: tuple[str, ...] = ()
    covering_tests: tuple[TraceTestRef, ...] = ()
    coverage_state: Literal["covered", "uncovered", "not_required"]
    latest_execution: TraceExecution | None = None
    freshest_pass: TraceExecution | None = None
    presence_in_current_batch: Literal["executed", "not_in_current_batch", "target_not_selected"]
    atemporal_kinds_present: tuple[str, ...] = ()
    failures: tuple[TraceFailure, ...] = ()
    open_problem_ids: tuple[str, ...] = ()


class TraceSource(BaseModel):
    model_config = _FROZEN

    path: str
    exists: bool
    sha256: str | None = None


class TraceProjection(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...] = ()
    rows: tuple[TraceRow, ...] = ()
    unmapped_tests: tuple[UnmappedTest, ...] = ()
    gaps: tuple[TraceGap, ...] = ()
    integrity: TraceIntegrity
