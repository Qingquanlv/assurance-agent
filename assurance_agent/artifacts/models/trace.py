"""inspect/trace-projection.json (must_compat): traceability fact projection.

Transcribed from spec v3 §6 (`docs/superpowers/specs/2026-07-29-traceability-
evidence-projection-design.md`) with three plan-level amendments:

- ``TraceRow.failures`` is a tuple (D5), superseding spec's singular
  ``failure``: the same case can carry several ``FailureEntry`` rows within
  one reconciled batch, kept in the authoritative order they appear in the
  input bytes (that input order is itself deterministic, so no further
  sorting rule is needed).
- ``TraceExecution`` carries ``ts_source`` recording which raw field supplied
  ``ts`` (an explicit ``executed_at`` vs. a legacy UTC value parsed out of
  ``batch_id``); the timezone/recency policy that consumes this is Task 3,
  not this module.
- ``TraceRow.problem_facts`` carries the §9.4/§9.5 join *facts* — every
  canonical Problem a case reaches, whatever its status or classification —
  next to ``open_problem_ids``, which stays the deduped routing subset. Spec
  §9 asks for both ("``resolved``/``not_an_issue``/``accepted_risk`` are
  recorded as facts but do not count as open", "test_bug/environment_issue
  record the fact without blocking"), and a projection that only published the
  routing subset would force every consumer that needs a different policy —
  the report's risk view treats ``accepted_risk`` as active, for instance — to
  re-read the ledger. The field defaults to ``()`` so a projection written
  before it still validates.

``TraceGap.code`` is a closed enum: the ten codes spec v3 §6 documents, plus
two plan additions — ``result_identity_mismatch`` (a result file's own
change/batch identity does not match the manifest that selected it) and
``problem_alias_invalid`` (a problem merge/alias reference is a cycle or
points at a problem_id that does not exist).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True, extra="forbid")

TraceTarget = Literal["api", "e2e", "fuzz", "performance"]
TraceCaseType = Literal["API", "E2E", "Fuzz", "Performance"]
# Named because a second artifact carries the same three levels
# (`trace_sufficiency.TraceSufficiencyFacts.integrity`), and two inline copies of
# a closed vocabulary is one copy too many to keep in step.
TraceIntegrity = Literal["complete", "complete_with_gaps", "incomplete"]
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


class TraceTestRef(BaseModel):
    """One current-tree test function mapped to a case_id (spec §7 scan hit)."""

    model_config = _FROZEN

    file: str
    test_name: str


class UnmappedTest(BaseModel):
    """A test that executed but did not resolve to any case_id via
    ``extract_case_id`` (spec §5's minimal evidence-side DTO). Distinct from
    ``TraceTestRef``, which records a *current-tree* scan hit that did
    resolve to a case_id (spec §7)."""

    model_config = _FROZEN

    file: str
    test_name: str


class TraceExecution(BaseModel):
    model_config = _FROZEN

    batch_id: str
    target: TraceTarget
    status: Literal["passed", "failed", "skipped"]
    ts: datetime
    ts_source: Literal["executed_at", "batch_id_legacy_utc"]


class TraceFailure(BaseModel):
    """Populated only when phase=reconciled; category is a free string (e.g.
    ``classification_unavailable`` for performance, per spec M3)."""

    model_config = _FROZEN

    category: str
    severity: str


class TraceProblemFact(BaseModel):
    """One canonical Problem a case reaches through the §9 two-hop join.

    Populated only when phase=reconciled. ``problem_id`` is the *canonical*
    problem — the end of the ``merged_into`` alias chain — and
    ``source_problem_ids`` lists every problem_id the change's occurrences
    actually referenced to get there (including the canonical id itself when an
    occurrence named it directly), sorted, so the trail back to the occurrence
    survives the alias collapse.

    ``status`` and ``classification`` are free strings, copied from the ledger
    verbatim: narrowing them to today's literals would turn a ledger written by
    a later release into a corrupt document. ``open_product_bug`` is this
    projection's §9.4 verdict on those two values, recorded so a consumer never
    has to re-derive the allowlist.
    """

    model_config = _FROZEN

    problem_id: str
    source_problem_ids: tuple[str, ...]
    fingerprint: str
    status: str
    classification: str
    open_product_bug: bool


class TraceGap(BaseModel):
    model_config = _FROZEN

    code: TraceGapCode
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class TraceRow(BaseModel):
    model_config = _FROZEN

    case_id: str
    module: str
    case_type: TraceCaseType
    automation_required: bool
    assertions: tuple[str, ...]
    covering_tests: tuple[TraceTestRef, ...]
    coverage_state: Literal["covered", "uncovered", "not_required"]
    latest_execution: TraceExecution | None
    freshest_pass: TraceExecution | None
    presence_in_current_batch: Literal["executed", "not_in_current_batch", "target_not_selected"]
    atemporal_kinds_present: tuple[str, ...]
    failures: tuple[TraceFailure, ...] = ()
    # The routing subset: `problem_facts` entries whose `open_product_bug` is
    # true, deduplicated by fingerprint (spec §9.5) — see `evidence/trace.py`.
    open_problem_ids: tuple[str, ...]
    problem_facts: tuple[TraceProblemFact, ...] = ()


class TraceSource(BaseModel):
    model_config = _FROZEN

    path: str
    exists: bool
    sha256: str | None = None


class TraceProjection(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...]
    rows: tuple[TraceRow, ...]
    unmapped_tests: tuple[UnmappedTest, ...]
    gaps: tuple[TraceGap, ...]
    integrity: TraceIntegrity


__all__ = [
    "TraceCaseType",
    "TraceExecution",
    "TraceFailure",
    "TraceGap",
    "TraceGapCode",
    "TraceIntegrity",
    "TraceProblemFact",
    "TraceProjection",
    "TraceRow",
    "TraceSource",
    "TraceTarget",
    "TraceTestRef",
    "UnmappedTest",
]
