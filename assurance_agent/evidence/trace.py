"""Fold on-disk change artifacts into a `TraceProjection` (spec v3 §6/§8).

The fold is policy-free and clock-free: it only reads documents, so the same
source set always folds to the same bytes. Recency, sufficiency and every
`as_of`-dependent judgement live in ``evidence/sufficiency.py``.

Two facts about the current batch are unavailable at the same time in the two
call sites, which shapes this module:

- ``aa trace`` / ``aa verify`` / the materialize node fold *after* the run, so
  they read ``execution/execution-manifest.yaml``.
- The runner folds *before* publishing that manifest (it needs the projection to
  build the quality gate), so it injects the same facts as
  ``ExecutionFoldInput``.

Both modes therefore project the batch onto one *logical manifest view* — the
five fold-relevant fields — and record it as a single source whose digest is the
canonical JSON of that view rather than the raw manifest bytes (deviation D6).
Hashing the file itself would make the two modes disagree, because the on-disk
manifest also carries gate-time fields (``final_status``) that do not exist yet
when the runner folds.

Coverage is the one fact no document can supply: a result file proves a test
*ran*, never that it still exists. So the fold also reads the SUT's current
``tests/`` tree through ``evidence/tree_scan.py`` and compares that scan's
per-file SHA-256 against the view's ``test_files_sha256``, file by file. Both
modes run that same comparison, and every disagreement — an added, deleted or
changed file — is one ``tests_tree_digest_mismatch``.

The ``reconciled`` phase (spec §9) is the *same* fold plus an enrichment pass:
it runs every step above, then reads three more documents — the change's
``inspect/failure-analysis.json`` and ``issues/snapshot.json``, and the
project's ``qa/issues/problems.json`` — and copies each row with its three
reconciled fields filled in: ``failures``, ``problem_facts`` (every canonical
Problem the case reaches, §9.4/§9.5) and ``open_problem_ids`` (the routing
subset of those facts). The execution phase initializes all three to empty
explicitly, so "no reconciled facts" is a stated fact rather than a default.
Enrichment therefore only ever *adds*: a row's execution-phase facts are
produced by one shared code path and carried over untouched, which is the spec's
dual-temporal invariant.

Every reconciled input is validated before it is believed, and a document the
fold refuses contributes nothing rather than raising. Two kinds of problem are
reported differently on purpose:

- The input *document* is unusable — absent, unparseable, schema-violating, or
  addressed to another change or batch. ``TraceGap.code`` is closed, so these
  share one code per input and the ``detail`` prefix (``missing:`` vs
  ``invalid:``) carries the distinction machine-readably.
- A *reference inside* a document the fold read successfully is broken — a merge
  alias cycle, or an occurrence naming a problem_id the ledger lacks. These are
  ``problem_alias_invalid`` and deliberately carry **neither** prefix: the ledger
  itself is present and valid, and prefixing them would tell a consumer to go
  looking for a document that is not the problem.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.artifacts.batch_id import (
    BATCH_ID_FORMAT_DESCRIPTION,
    BatchIdInstant,
    parse_batch_id as parse_batch_id_instant,
)
from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.execution import SelectedTargets
from assurance_agent.artifacts.models.trace import (
    TraceCaseType,
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceGapCode,
    TraceProblemFact,
    TraceProjection,
    TraceRow,
    TraceSource,
    TraceTarget,
    TraceTestRef,
    UnmappedTest,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.evidence.case_doc import EvidenceCaseEntry, load_case_entries
from assurance_agent.evidence.tree_scan import TreeScanResult, scan_test_tree

TraceStatus = Literal["passed", "failed", "skipped"]
TsSource = Literal["executed_at", "batch_id_legacy_utc"]
_DocumentT = TypeVar("_DocumentT", bound=BaseModel)

MANIFEST_FOLD_VIEW_SOURCE = "execution/execution-manifest.yaml#fold-view"
TESTS_TREE_SCAN_SOURCE = "tests/#tree-scan"
FAILURE_ANALYSIS_SOURCE = "inspect/failure-analysis.json"
ISSUES_SNAPSHOT_SOURCE = "issues/snapshot.json"
# The one input that is not change-relative, so the prefix keeps it from reading
# as a path inside the change directory.
PROJECT_PROBLEMS_SOURCE = "project:qa/issues/problems.json"
# `TraceGap.code` is closed, so the three reconciled inputs share one code each
# for "no usable document". These prefixes make the difference the code cannot
# express machine-readable: the input was absent, or it was present and refused.
GAP_DETAIL_MISSING = "missing:"
GAP_DETAIL_INVALID = "invalid:"

_PYTEST_TARGETS: tuple[TraceTarget, ...] = ("api", "e2e", "fuzz")
_ALL_TARGETS: tuple[TraceTarget, ...] = ("api", "e2e", "fuzz", "performance")
_CASE_TYPE_TARGET: Mapping[TraceCaseType, TraceTarget] = {
    "API": "api",
    "E2E": "e2e",
    "Fuzz": "fuzz",
    "Performance": "performance",
}
_PERF_VERDICT_STATUS: Mapping[str, TraceStatus] = {
    "PASS": "passed",
    "FAIL": "failed",
    "SKIPPED": "skipped",
}
# A case is only "passed" when nothing about it failed; a batch that merely
# skipped it is the weakest fact.
_STATUS_RANK: Mapping[TraceStatus, int] = {"skipped": 0, "passed": 1, "failed": 2}
# The only gap that describes the rows rather than the fold's inputs, so the
# only one a sufficiency policy is allowed to soften (see `_integrity`).
_DEGRADING_GAP_CODES: frozenset[TraceGapCode] = frozenset({"mapped_test_missing_from_tree"})
# spec §9.4 / D4: the closed statuses, and the one classification that blocks.
# Everything else is a fact the row records without counting it open.
_CLOSED_PROBLEM_STATUSES: frozenset[str] = frozenset({"resolved", "not_an_issue", "accepted_risk"})
_OPEN_CLASSIFICATION = "product_bug"
# The two snapshot self-reports that make an Issue snapshot answerable at all
# (`IssueAnalysisStatusValue` / `ProjectSyncStatus` in artifacts/models/issues.py).
_COMPLETED_ANALYSIS_STATUS = "completed"
_SYNCED_PROJECT_STATUS = "completed"
# A7: the projection writes exactly this disposition when a Problem is merged
# (`workflow/issues/projection.py`), so only this exact form is an alias. Any
# other disposition is a human sentence and must not be parsed as one.
_MERGE_ALIAS_PREFIX = "merged_into:"


# --------------------------------------------------------------------------- #
# read-only DTOs (spec §5: take the fields, copy none of the semantics)
# --------------------------------------------------------------------------- #


class ResultTestRow(BaseModel):
    """One executed test row inside a pytest-target result document."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: str
    status: TraceStatus
    file: str
    test_name: str


class ResultUnmappedRow(BaseModel):
    """A result document's unmapped-test row.

    Kept separate from ``UnmappedTest``: the projection model forbids extras (it
    is an authoritative artifact), while on-disk rows are full ``CaseResult``
    records carrying status, timings and log references.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    file: str
    test_name: str


class ResultDocument(BaseModel):
    """api/e2e/fuzz result identity header plus the minimal row set.

    The identity fields are required — they are what the fold checks the document
    against — while the row lists default to empty: a target that produced no
    rows is empty evidence, not a corrupt document.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    change_id: str
    batch_id: str
    target: Literal["api", "e2e", "fuzz"]
    cases: list[ResultTestRow] = Field(default_factory=list)
    unmapped_tests: list[ResultUnmappedRow] = Field(default_factory=list)


class PerformanceScenarioRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    capability: str
    verdict: Literal["PASS", "FAIL", "SKIPPED"]


class PerformanceResultDocument(BaseModel):
    """performance results are `kind + scenarios`, not `target + cases` (D7)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    change_id: str
    batch_id: str
    kind: Literal["performance"]
    scenarios: list[PerformanceScenarioRow] = Field(default_factory=list)


class FailureRow(BaseModel):
    """One ``FailureEntry`` row, reduced to the three fields the fold projects.

    ``category`` and ``severity`` stay free strings on purpose: ``TraceFailure``
    declares them that way (spec §6), so a category the classifier learns after
    this release is a fact to record, not a document to reject. ``case_id`` is
    required — a failure that names no case cannot be attributed to a row, and
    silently dropping it would understate the evidence.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: str
    category: str
    severity: str


class FailureAnalysisDocument(BaseModel):
    """``inspect/failure-analysis.json``, as the fold reads it.

    Only the authoritative ``failures`` list is consumed: ``hard_fails``,
    ``needs_review`` and ``known_product_issues`` are filtered views of it
    (``workflow/report/inspector.py``), so reading them too would duplicate rows.

    ``schema_version`` is pinned exactly as ``FailureAnalysis`` pins it: a future
    version may have moved the very fields this fold projects, and guessing is
    worse than reporting that the input is unusable. Both batch fields are
    required because both are checked — see ``_read_failures``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    source_batch_id: str
    failures: list[FailureRow] = Field(default_factory=list)


class SnapshotObservationRow(BaseModel):
    """An Observation, reduced to the case link the join's first hop needs.

    ``batch_id`` is required because the join's answer depends on it: an
    observation the last completed analysis could not have seen has no
    occurrence and no Problem, and reading its absence as "no open problem"
    would be a fail-open (see ``IssuesSnapshotDocument.degraded_reason``).
    ``Observation`` requires the field, so a row without one is not an
    Observation this fold can place in time.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    observation_id: str
    batch_id: str
    case_id: str | None = None


class SnapshotOccurrenceRow(BaseModel):
    """An IssueOccurrence, reduced to the join's second hop."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    observation_ids: list[str] = Field(default_factory=list)
    problem_id: str


class SnapshotAnalysisStatusRow(BaseModel):
    """The Issue analysis' own verdict on whether it finished, and for which batch.

    ``status`` stays a free string: any value other than ``completed`` means the
    snapshot cannot be trusted, so an unrecognized one fails closed by default
    instead of failing validation.

    ``batch_id`` is required for the same reason it is required on
    ``IssueAnalysisStatus``: a completed analysis that does not say *what* it
    analysed cannot be checked against the observations on record.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: str
    batch_id: str


class IssuesSnapshotDocument(BaseModel):
    """``issues/snapshot.json``, as the fold reads it.

    The two self-reported statuses are read because the join's answer depends on
    them: an analysis that never completed has not produced the occurrences this
    fold walks, and a project sync that is still pending means the ledger the
    fold reads does not yet contain the Problems those occurrences point at.
    ``report_builder`` already reads exactly these two fields as ``unknown``
    Issue risk; a projection that folded them as fact would claim a case has no
    open problem when the truth is that nobody has looked yet.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    change_id: str
    observations: list[SnapshotObservationRow] = Field(default_factory=list)
    occurrences: list[SnapshotOccurrenceRow] = Field(default_factory=list)
    analysis_status: SnapshotAnalysisStatusRow | None = None
    # `ChangeIssueSnapshot` defaults this to "completed" for snapshots written
    # before the field existed; the fold inherits that default rather than
    # inventing a stricter one.
    project_sync_status: str = _SYNCED_PROJECT_STATUS

    def degraded_reason(self) -> str:
        """Why this snapshot cannot answer the join, or "" when it can.

        An *explicit* non-``completed`` analysis status and a non-``completed``
        project sync are degraded unconditionally. A **null** ``analysis_status``
        is not: a clean batch produces no abnormal observations, so no analysis
        is ever started and the field stays null
        (``collect_observations_operation`` writes exactly that snapshot — no
        observations, no occurrences, null analysis, completed sync). Treating it
        as degraded would deny every green change a usable reconciled projection.
        With observations present the null means the opposite — an analysis was
        owed and did not land — which is where ``report_builder``'s reading of a
        null status as ``failed`` applies.
        """
        if self.analysis_status is None:
            if self.observations:
                return (
                    f"analysis_status is absent for {len(self.observations)} collected observation(s), "
                    "so no Issue analysis completed for them"
                )
        elif self.analysis_status.status != _COMPLETED_ANALYSIS_STATUS:
            return f"analysis_status.status is {self.analysis_status.status!r}, not 'completed'"
        else:
            unanalyzed = self._unanalyzed_batches(self.analysis_status.batch_id)
            if unanalyzed:
                return (
                    f"analysis_status.batch_id is {self.analysis_status.batch_id!r}, which does not "
                    f"cover observation batch(es) {', '.join(repr(batch) for batch in unanalyzed)}"
                )
        if self.project_sync_status != _SYNCED_PROJECT_STATUS:
            return f"project_sync_status is {self.project_sync_status!r}, not 'completed'"
        return ""

    def _unanalyzed_batches(self, analysed_batch_id: str) -> tuple[str, ...]:
        """Observation batches the completed analysis cannot speak for.

        A snapshot is folded from the change's whole issue event log, so
        observations accumulate across batches while ``analysis_status``
        describes only the newest analysis
        (``workflow/issues/projection.py``). An observation from an *earlier*
        batch was analysed in its own run and its occurrences are already on
        record; one from a *later* batch did not exist when this analysis ran, so
        it has no occurrence — and reading that absence as "no open problem"
        would claim a verdict nobody reached.

        Ordering is the batch-id timestamp format, which sorts
        lexicographically. Two ids that do not both parse cannot be ordered at
        all, so any difference between them counts as unanalyzed rather than
        guessed in the direction that passes.
        """
        analysed = _batch_order_key(analysed_batch_id)
        unanalyzed = {
            observation.batch_id
            for observation in self.observations
            if observation.batch_id != analysed_batch_id
            and not _is_analysed_before(observation.batch_id, analysed_batch_id, analysed)
        }
        return tuple(sorted(unanalyzed))


class ProblemFingerprintRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    digest: str


class ProblemAssessmentRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    classification: str


class ProblemResolutionRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    disposition: str


class ProblemRow(BaseModel):
    """A Problem, reduced to identity, status, classification and alias target.

    ``fingerprint`` and ``assessment`` are required rather than defaulted: the
    ledger's own model requires both, and a fold that defaulted the digest would
    collapse unrelated Problems in the fingerprint dedupe. A document that omits
    them is refused as a whole instead.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    problem_id: str
    status: str
    fingerprint: ProblemFingerprintRow
    assessment: ProblemAssessmentRow
    resolution: ProblemResolutionRow | None = None

    def merge_target(self) -> str | None:
        """The problem this one merged into, or ``None`` if it is canonical."""
        disposition = self.resolution.disposition if self.resolution is not None else ""
        if not disposition.startswith(_MERGE_ALIAS_PREFIX):
            return None
        return disposition[len(_MERGE_ALIAS_PREFIX) :]

    def is_open(self) -> bool:
        """Spec §9.4 / D4, evaluated on a *canonical* problem only.

        An unrecognized status counts as open: the closed set is the allowlist,
        so a status this release has never heard of can never close a Problem.
        """
        return (
            self.status not in _CLOSED_PROBLEM_STATUSES
            and self.assessment.classification == _OPEN_CLASSIFICATION
        )


class ProblemLedgerDocument(BaseModel):
    """``qa/issues/problems.json`` (project-level), as the fold reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    problems: list[ProblemRow] = Field(default_factory=list)


class ManifestFoldView(BaseModel):
    """The five manifest fields the fold consumes, and nothing else.

    ``executed_at`` is ``AwareDatetime``: a manifest that records a naive
    timestamp is rejected rather than localized to a guessed zone, because the
    whole point of the field is to replace the batch-id approximation.

    **Byte parity is a precondition on the writer, not on this fold.** The
    injected view always carries an aware ``executed_at`` and a (possibly empty)
    ``test_files_sha256`` mapping, so a published manifest that leaves either
    field null projects onto a *different* view and digests differently. Task 8's
    manifest writer must therefore publish both fields non-null with the same
    values the runner injected; this fold deliberately does not paper over the
    difference by normalizing null to a default, because that would hide the
    writer's omission behind an identical digest. Legacy manifests predating
    those fields keep working — they simply fall back to the batch-id UTC
    approximation and are never compared against an injected fold.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    change_id: str
    batch_id: str
    executed_at: AwareDatetime | None = None
    selected_targets: SelectedTargets
    test_files_sha256: dict[str, str] | None = None

    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


@dataclass(frozen=True)
class ExecutionFoldInput:
    """Current-batch facts the runner injects before the manifest exists.

    ``executed_at`` must be the same aware value the runner then writes to the
    manifest, so the pre-publish fold and any later on-disk fold agree (see
    ``ManifestFoldView`` for the full parity precondition).
    """

    batch_id: str
    executed_at: datetime
    selected_targets: SelectedTargets
    test_files_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.executed_at.utcoffset() is None:
            raise ValueError(f"ExecutionFoldInput.executed_at must be timezone-aware: {self.executed_at!r}")
        # The runner passes its live tree-hash mapping; snapshot it so a later
        # mutation there cannot change what this batch claims to have executed.
        object.__setattr__(self, "test_files_sha256", dict(self.test_files_sha256))


# --------------------------------------------------------------------------- #
# internal fold state
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _BatchFacts:
    batch_id: str
    ts: datetime
    ts_source: TsSource
    case_statuses: Mapping[tuple[str, TraceTarget], TraceStatus]
    perf_statuses: Mapping[str, TraceStatus]
    unmapped: tuple[UnmappedTest, ...]
    # Which test functions this batch resolved to each case_id. Kept so the
    # fold can tell a mapping that disappeared from the tree from one that was
    # never there; performance has no test functions, so it contributes none.
    case_tests: Mapping[str, tuple[TraceTestRef, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Observation:
    batch: _BatchFacts
    target: TraceTarget
    status: TraceStatus

    def execution(self) -> TraceExecution:
        return TraceExecution(
            batch_id=self.batch.batch_id,
            target=self.target,
            status=self.status,
            ts=self.batch.ts,
            ts_source=self.batch.ts_source,
        )


@dataclass
class _Fold:
    """Accumulates the gaps and sources produced while reading documents."""

    gaps: list[TraceGap] = field(default_factory=list)
    sources: list[TraceSource] = field(default_factory=list)

    def gap(
        self,
        code: TraceGapCode,
        source: str,
        *,
        batch_id: str | None = None,
        target: str | None = None,
        detail: str = "",
    ) -> None:
        self.gaps.append(TraceGap(code=code, source=source, batch_id=batch_id, target=target, detail=detail))

    def read_source(self, path: Path, rel: str) -> None:
        try:
            digest: str | None = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        self.sources.append(TraceSource(path=rel, exists=True, sha256=digest))

    def missing_source(self, rel: str) -> None:
        self.sources.append(TraceSource(path=rel, exists=False, sha256=None))


# --------------------------------------------------------------------------- #
# document readers
# --------------------------------------------------------------------------- #


def _parse_batch_id(batch_id: str) -> datetime | None:
    """Fallback recency source: a time-encoded batch id, stamped UTC.

    The value is an approximation (the batch id carries no zone), which is why
    rows built from it report ``ts_source="batch_id_legacy_utc"``.
    """
    parsed = parse_batch_id_instant(batch_id)
    return parsed.as_datetime() if parsed is not None else None


def _batch_order_key(batch_id: str) -> BatchIdInstant | None:
    return parse_batch_id_instant(batch_id)


def _is_analysed_before(
    batch_id: str,
    analysed_batch_id: str,
    analysed: BatchIdInstant | None,
) -> bool:
    """Whether ``batch_id`` is strictly older than the analysed batch.

    ``False`` whenever the two cannot be ordered — an unparseable id on either
    side — because "not older" is the fail-closed answer: it makes the caller
    treat the difference as unanalyzed rather than assume the analysis covered
    it. Both ids are parsed rather than compared as strings so a non-batch
    directory name can never sort itself under a real batch.
    """
    if analysed is None:
        return False
    parsed = _batch_order_key(batch_id)
    if parsed is None:
        return False
    return parsed < analysed and batch_id != analysed_batch_id


def _read_json_object(path: Path) -> tuple[dict[str, object] | None, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(document, dict):
        return None, "document is not a JSON object"
    return document, ""


def _identity_error(document: Mapping[str, object], expected: Sequence[tuple[str, str]]) -> str:
    """Return the first identity field that does not match, or "" if all do."""
    for field_name, wanted in expected:
        found = document.get(field_name)
        if found != wanted:
            return f"{field_name}: expected {wanted!r}, found {found!r}"
    return ""


def _read_manifest_view(path: Path, change_id: str) -> tuple[ManifestFoldView | None, str]:
    """Read a manifest as the fold view, rejecting one that belongs elsewhere."""
    if not path.is_file():
        return None, f"{path.name} not found"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, str(exc)
    if not isinstance(document, dict):
        return None, f"{path.name} is not a YAML mapping"
    try:
        view = ManifestFoldView.model_validate(document)
    except ValidationError as exc:
        return None, str(exc)
    if view.change_id != change_id:
        return None, f"change_id: expected {change_id!r}, found {view.change_id!r}"
    return view, ""


def _current_view(
    change_dir: Path,
    change_id: str,
    current: ExecutionFoldInput | None,
    fold: _Fold,
) -> ManifestFoldView | None:
    """Project the current batch onto the logical manifest view (D6).

    An injected view is by construction present, so the injected mode can never
    report ``manifest_missing`` for a manifest it deliberately does not read.
    """
    if current is not None:
        return ManifestFoldView(
            change_id=change_id,
            batch_id=current.batch_id,
            executed_at=current.executed_at,
            selected_targets=current.selected_targets,
            test_files_sha256=dict(current.test_files_sha256),
        )

    view, error = _read_manifest_view(change_dir / "execution" / "execution-manifest.yaml", change_id)
    if view is None:
        fold.gap("manifest_missing", MANIFEST_FOLD_VIEW_SOURCE, detail=error)
    return view


def _case_sources(change_dir: Path, fold: _Fold) -> None:
    for path in sorted(change_dir.glob("cases/**/case.yaml")):
        fold.read_source(path, path.relative_to(change_dir).as_posix())


def _aggregate(statuses: Iterable[TraceStatus]) -> TraceStatus:
    return max(statuses, key=lambda status: _STATUS_RANK[status])


def _load_target_result(
    path: Path,
    rel: str,
    *,
    change_id: str,
    batch_id: str,
    target: TraceTarget,
    fold: _Fold,
) -> ResultDocument | None:
    fold.read_source(path, rel)
    document, error = _read_json_object(path)
    if document is None:
        fold.gap("result_corrupt", rel, batch_id=batch_id, target=target, detail=error)
        return None
    mismatch = _identity_error(
        document, (("change_id", change_id), ("batch_id", batch_id), ("target", target))
    )
    if mismatch:
        fold.gap("result_identity_mismatch", rel, batch_id=batch_id, target=target, detail=mismatch)
        return None
    try:
        return ResultDocument.model_validate(document)
    except ValidationError as exc:
        fold.gap("result_corrupt", rel, batch_id=batch_id, target=target, detail=str(exc))
        return None


def _load_performance_result(
    path: Path,
    rel: str,
    *,
    change_id: str,
    batch_id: str,
    fold: _Fold,
) -> PerformanceResultDocument | None:
    fold.read_source(path, rel)
    document, error = _read_json_object(path)
    if document is None:
        fold.gap("result_corrupt", rel, batch_id=batch_id, target="performance", detail=error)
        return None
    mismatch = _identity_error(
        document, (("change_id", change_id), ("batch_id", batch_id), ("kind", "performance"))
    )
    if mismatch:
        fold.gap("result_identity_mismatch", rel, batch_id=batch_id, target="performance", detail=mismatch)
        return None
    try:
        return PerformanceResultDocument.model_validate(document)
    except ValidationError as exc:
        fold.gap("result_corrupt", rel, batch_id=batch_id, target="performance", detail=str(exc))
        return None


def _batch_timestamp(
    change_dir: Path,
    batch_id: str,
    legacy_ts: datetime,
    view: ManifestFoldView | None,
    change_id: str,
    fold: _Fold,
) -> tuple[datetime, TsSource]:
    """Resolve one batch's timestamp: explicit ``executed_at`` first (P1-5).

    The current batch takes its value from the logical view — never from
    ``runs/<batch>/execution-manifest.yaml``, which does not exist yet when the
    runner folds and would otherwise make the two modes read different sources.
    """
    if view is not None and batch_id == view.batch_id:
        if view.executed_at is not None:
            return view.executed_at, "executed_at"
    else:
        path = change_dir / "execution" / "runs" / batch_id / "execution-manifest.yaml"
        if path.is_file():
            fold.read_source(path, f"execution/runs/{batch_id}/execution-manifest.yaml")
            historical, _ = _read_manifest_view(path, change_id)
            if historical is not None and historical.executed_at is not None:
                return historical.executed_at, "executed_at"

    return legacy_ts, "batch_id_legacy_utc"


def _read_batches(
    change_dir: Path,
    change_id: str,
    view: ManifestFoldView | None,
    fold: _Fold,
) -> list[_BatchFacts]:
    runs_dir = change_dir / "execution" / "runs"
    if not runs_dir.is_dir():
        return []

    batches: list[_BatchFacts] = []
    for entry in sorted(runs_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_dir():
            continue
        batch_id = entry.name
        legacy_ts = _parse_batch_id(batch_id)
        if legacy_ts is None:
            fold.gap(
                "batch_id_unparseable",
                f"execution/runs/{batch_id}",
                batch_id=batch_id,
                detail=f"directory name is not {BATCH_ID_FORMAT_DESCRIPTION}",
            )
            continue

        ts, ts_source = _batch_timestamp(change_dir, batch_id, legacy_ts, view, change_id, fold)
        rows: dict[tuple[str, TraceTarget], list[TraceStatus]] = {}
        perf_rows: dict[str, list[TraceStatus]] = {}
        unmapped: set[tuple[str, str]] = set()
        case_tests: dict[str, set[tuple[str, str]]] = {}

        for target in _PYTEST_TARGETS:
            path = entry / f"{target}-result.json"
            if not path.is_file():
                continue
            document = _load_target_result(
                path,
                f"execution/runs/{batch_id}/{target}-result.json",
                change_id=change_id,
                batch_id=batch_id,
                target=target,
                fold=fold,
            )
            if document is None:
                continue
            for case in document.cases:
                rows.setdefault((case.case_id, target), []).append(case.status)
                case_tests.setdefault(case.case_id, set()).add((case.file, case.test_name))
            unmapped.update((row.file, row.test_name) for row in document.unmapped_tests)

        perf_path = entry / "performance-result.json"
        if perf_path.is_file():
            perf_document = _load_performance_result(
                perf_path,
                f"execution/runs/{batch_id}/performance-result.json",
                change_id=change_id,
                batch_id=batch_id,
                fold=fold,
            )
            if perf_document is not None:
                for scenario in perf_document.scenarios:
                    perf_rows.setdefault(scenario.capability, []).append(
                        _PERF_VERDICT_STATUS[scenario.verdict]
                    )

        batches.append(
            _BatchFacts(
                batch_id=batch_id,
                ts=ts,
                ts_source=ts_source,
                case_statuses={key: _aggregate(values) for key, values in rows.items()},
                perf_statuses={key: _aggregate(values) for key, values in perf_rows.items()},
                unmapped=tuple(UnmappedTest(file=file, test_name=name) for file, name in sorted(unmapped)),
                case_tests={
                    case_id: tuple(TraceTestRef(file=file, test_name=name) for file, name in sorted(refs))
                    for case_id, refs in sorted(case_tests.items())
                },
            )
        )
    return batches


def _report_missing_current_results(
    view: ManifestFoldView | None,
    change_dir: Path,
    fold: _Fold,
) -> None:
    """A target the current batch selected must have produced a result file.

    Only the current batch is checked: historical batches selected whatever they
    selected, and re-litigating that would bury the current gap in noise.
    """
    if view is None:
        return
    if _parse_batch_id(view.batch_id) is None:
        fold.gap(
            "batch_id_unparseable",
            MANIFEST_FOLD_VIEW_SOURCE,
            batch_id=view.batch_id,
            detail=f"authoritative batch id is not {BATCH_ID_FORMAT_DESCRIPTION}",
        )
        return

    batch_dir = change_dir / "execution" / "runs" / view.batch_id
    for target in _ALL_TARGETS:
        if not getattr(view.selected_targets, target):
            continue
        path = batch_dir / f"{target}-result.json"
        if path.is_file():
            # Present but unusable documents already reported corrupt/mismatch.
            continue
        rel = f"execution/runs/{view.batch_id}/{target}-result.json"
        fold.missing_source(rel)
        fold.gap("result_missing", rel, batch_id=view.batch_id, target=target)


# --------------------------------------------------------------------------- #
# row assembly
# --------------------------------------------------------------------------- #


def _drift_detail(added: Sequence[str], deleted: Sequence[str], changed: Sequence[str]) -> str:
    sections = (("added", added), ("deleted", deleted), ("changed", changed))
    return "; ".join(f"{label}: {', '.join(paths)}" for label, paths in sections if paths)


def _report_tree_drift(view: ManifestFoldView | None, tree: TreeScanResult, fold: _Fold) -> None:
    """Compare the batch's per-file hashes against the current tree (D2'/D6).

    Both fold modes run this same comparison — the on-disk view's mapping comes
    from the manifest, the injected one from ``ExecutionFoldInput`` — and both
    compare plain per-file SHA-256 values, so there is no aggregation algorithm
    to disagree about. A view that recorded no mapping at all claims an empty
    tree: every current test file is then reported as added, because a manifest
    without ``test_files_sha256`` cannot vouch for what ran (see
    ``ManifestFoldView``'s writer precondition). Without a view there is nothing
    to compare, and ``manifest_missing`` already says so.
    """
    if view is None:
        return
    baseline = view.test_files_sha256 or {}
    current = tree.file_sha256
    added = sorted(set(current) - set(baseline))
    deleted = sorted(set(baseline) - set(current))
    changed = sorted(path for path in set(baseline) & set(current) if baseline[path] != current[path])
    if not (added or deleted or changed):
        return
    fold.gap(
        "tests_tree_digest_mismatch",
        TESTS_TREE_SCAN_SOURCE,
        batch_id=view.batch_id,
        detail=_drift_detail(added, deleted, changed),
    )


def _report_missing_mapped_tests(
    entry: EvidenceCaseEntry,
    row: TraceRow,
    batches: Sequence[_BatchFacts],
    tree_functions: frozenset[TraceTestRef],
    fold: _Fold,
) -> None:
    """A mapping the newest batch executed that no longer exists in the tree.

    Only reported when the case ends up ``uncovered``. A test renamed while
    keeping its case id still maps the case (spec §8, identity is the case id),
    so flagging that benign rename would bury the case this gap exists for — the
    test was deleted, and the evidence of coverage is gone. ``not_required`` is
    excluded for the opposite reason: nobody asked that case to be automated, so
    a mapping it happens to have lost is not a gap in the evidence.
    """
    if row.coverage_state != "uncovered":
        return
    executed = [batch for batch in batches if entry.case_id in batch.case_tests]
    if not executed:
        return
    batch = max(executed, key=lambda item: item.batch_id)
    missing = [ref for ref in batch.case_tests[entry.case_id] if ref not in tree_functions]
    if not missing:
        return
    fold.gap(
        "mapped_test_missing_from_tree",
        TESTS_TREE_SCAN_SOURCE,
        batch_id=batch.batch_id,
        detail=f"{entry.case_id}: "
        + ", ".join(f"{ref.file}::{ref.test_name}" for ref in missing)
        + " no longer in the tests tree",
    )


def _observations(entry: EvidenceCaseEntry, batches: Sequence[_BatchFacts]) -> list[_Observation]:
    """Every (batch, target) result touching this case."""
    observations: list[_Observation] = []
    for batch in batches:
        for target in _PYTEST_TARGETS:
            status = batch.case_statuses.get((entry.case_id, target))
            if status is not None:
                observations.append(_Observation(batch, target, status))
        if entry.perf_capability is not None:
            status = batch.perf_statuses.get(entry.perf_capability)
            if status is not None:
                observations.append(_Observation(batch, "performance", status))
    return observations


def _speaks_for_batch(observations: Sequence[_Observation], own_target: TraceTarget) -> _Observation | None:
    """Pick the one observation that represents this case within one batch.

    A case declared as API is answered by the api suite: if the same case_id also
    ran under another target in that batch (a fuzz suite reusing the id, say),
    the case's own target still speaks for it. Only when the own target is absent
    does a fixed target order decide, so the choice never depends on which files
    happened to be read first.
    """
    if not observations:
        return None
    for item in observations:
        if item.target == own_target:
            return item
    return min(observations, key=lambda item: _ALL_TARGETS.index(item.target))


def _newest(observations: Sequence[_Observation], own_target: TraceTarget) -> _Observation | None:
    """The own-target-preferred observation from the highest batch id."""
    if not observations:
        return None
    newest_batch = max(item.batch.batch_id for item in observations)
    return _speaks_for_batch(
        [item for item in observations if item.batch.batch_id == newest_batch], own_target
    )


def _execution(observation: _Observation | None) -> TraceExecution | None:
    return observation.execution() if observation is not None else None


def _coverage_state(
    entry: EvidenceCaseEntry, tree: TreeScanResult
) -> Literal["covered", "uncovered", "not_required"]:
    covered = entry.case_id in tree.case_ids or (
        entry.perf_capability is not None and entry.perf_capability in tree.perf_capabilities
    )
    if covered:
        return "covered"
    return "uncovered" if entry.automation_required else "not_required"


def _atemporal_kinds(
    entry: EvidenceCaseEntry,
    observations: Sequence[_Observation],
    coverage_state: Literal["covered", "uncovered", "not_required"],
) -> tuple[str, ...]:
    kinds: list[str] = []
    if coverage_state == "covered":
        kinds.append("covered")
    if entry.type == "Fuzz":
        fuzz = _newest([item for item in observations if item.target == "fuzz"], "fuzz")
        if fuzz is not None and fuzz.status in ("passed", "failed"):
            kinds.append("fuzz_run")
    if entry.type == "Performance":
        perf = _newest([item for item in observations if item.target == "performance"], "performance")
        if perf is not None and perf.status != "skipped":
            kinds.append("perf_run")
    return tuple(kinds)


def _presence(
    entry: EvidenceCaseEntry,
    observations: Sequence[_Observation],
    view: ManifestFoldView | None,
) -> Literal["executed", "not_in_current_batch", "target_not_selected"]:
    if view is None:
        return "not_in_current_batch"
    if any(item.batch.batch_id == view.batch_id for item in observations):
        return "executed"
    if not getattr(view.selected_targets, _CASE_TYPE_TARGET[entry.type]):
        return "target_not_selected"
    return "not_in_current_batch"


def _build_row(
    entry: EvidenceCaseEntry,
    batches: Sequence[_BatchFacts],
    view: ManifestFoldView | None,
    tree: TreeScanResult,
) -> TraceRow:
    observations = _observations(entry, batches)
    own_target = _CASE_TYPE_TARGET[entry.type]
    coverage_state = _coverage_state(entry, tree)
    return TraceRow(
        case_id=entry.case_id,
        module=entry.module,
        case_type=entry.type,
        automation_required=entry.automation_required,
        assertions=entry.assertions,
        covering_tests=tree.covering_tests.get(entry.case_id, ()),
        coverage_state=coverage_state,
        latest_execution=_execution(_newest(observations, own_target)),
        freshest_pass=_execution(
            _newest([item for item in observations if item.status == "passed"], own_target)
        ),
        presence_in_current_batch=_presence(entry, observations, view),
        atemporal_kinds_present=_atemporal_kinds(entry, observations, coverage_state),
        # The three reconciled fields, stated empty rather than defaulted: an
        # execution-phase row asserts it has no reconciled facts yet, and the
        # enrichment pass is the only thing that may fill them in.
        failures=(),
        problem_facts=(),
        open_problem_ids=(),
    )


def _unique_entries(entries: Sequence[EvidenceCaseEntry]) -> list[EvidenceCaseEntry]:
    """One row per case_id; the first declaration (path-sorted) wins."""
    seen: dict[str, EvidenceCaseEntry] = {}
    for entry in entries:
        seen.setdefault(entry.case_id, entry)
    return sorted(seen.values(), key=lambda entry: entry.case_id)


def _integrity(
    rows: Sequence[TraceRow], gaps: Sequence[TraceGap]
) -> Literal["complete", "complete_with_gaps", "incomplete"]:
    """Spec §10, with exactly one softenable gap.

    ``mapped_test_missing_from_tree`` is a statement *about the rows* — the
    projection folded correctly and says, truthfully, that a case lost its test.
    A sufficiency policy may choose to warn on that. Every other code means an
    input was missing, corrupt or contradicted (including a tests tree that does
    not match what the batch executed), so the projection cannot be trusted as a
    whole and no policy may soften it. Zero rows is the same verdict: there is
    nothing to be complete about.
    """
    if not rows:
        return "incomplete"
    if any(gap.code not in _DEGRADING_GAP_CODES for gap in gaps):
        return "incomplete"
    return "complete_with_gaps" if gaps else "complete"


# --------------------------------------------------------------------------- #
# reconciled enrichment (spec §9)
# --------------------------------------------------------------------------- #


def _invalid_detail(rel: str, reason: str) -> str:
    """A gap detail for an input that exists but cannot be used."""
    return f"{GAP_DETAIL_INVALID}{rel}: {reason}"


def _read_fold_document(
    path: Path,
    rel: str,
    model: type[_DocumentT],
    *,
    code: TraceGapCode,
    fold: _Fold,
) -> _DocumentT | None:
    """Read one reconciled input, recording it as a source either way.

    Missing, unreadable, non-JSON and schema-violating documents all collapse to
    the same typed gap — ``TraceGap.code`` is closed, and from the projection's
    point of view all four mean "no usable document". The ``detail`` prefix
    (``missing:`` vs ``invalid:``) carries the one distinction a consumer needs
    to act on, machine-readably: an absent input may simply not have been
    produced yet, whereas a present one that this fold refused is a contradiction
    someone has to look at. Nothing here raises — a broken reconciled input must
    never cost the caller the execution facts the same fold already established.
    """
    if not path.is_file():
        fold.missing_source(rel)
        fold.gap(code, rel, detail=f"{GAP_DETAIL_MISSING}{rel}")
        return None
    fold.read_source(path, rel)
    document, error = _read_json_object(path)
    if document is None:
        fold.gap(code, rel, detail=_invalid_detail(rel, error))
        return None
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        fold.gap(code, rel, detail=_invalid_detail(rel, str(exc)))
        return None


def _read_failures(
    change_dir: Path,
    change_id: str,
    authoritative_batch_id: str,
    fold: _Fold,
) -> Mapping[str, tuple[TraceFailure, ...]]:
    """Group every ``FailureEntry`` by case, in document order (D5).

    No severity ranking and no de-duplication: two identical failures are two
    facts, and the input's order is itself deterministic, so preserving it is
    both the cheapest and the most faithful rule.

    Identity is checked twice over. The change must match, and both batch fields
    must name the projection's authoritative batch: ``batch_id`` is the batch the
    inspector ran for and ``source_batch_id`` the batch whose results it
    classified, and the archive gate already refuses to proceed when the latter
    disagrees with the execution batch (``workflow-schema.yaml``:
    ``failure_analysis.source_batch_id == execution.batch_id``). Folding a
    left-over analysis as this batch's failures would contradict that gate. With
    no authoritative batch there is nothing to check against, so the document is
    equally unusable — the projection would otherwise attribute failures to a
    batch it could not even name.
    """
    document = _read_fold_document(
        change_dir / "inspect" / "failure-analysis.json",
        FAILURE_ANALYSIS_SOURCE,
        FailureAnalysisDocument,
        code="failure_analysis_missing",
        fold=fold,
    )
    if document is None:
        return {}
    reason = _failure_identity_reason(document, change_id, authoritative_batch_id)
    if reason:
        fold.gap("failure_analysis_missing", FAILURE_ANALYSIS_SOURCE, detail=reason)
        return {}

    grouped: dict[str, list[TraceFailure]] = {}
    for row in document.failures:
        grouped.setdefault(row.case_id, []).append(TraceFailure(category=row.category, severity=row.severity))
    return {case_id: tuple(failures) for case_id, failures in grouped.items()}


def _failure_identity_reason(
    document: FailureAnalysisDocument,
    change_id: str,
    authoritative_batch_id: str,
) -> str:
    """The ``invalid:`` detail for a mis-addressed analysis, or "" if it fits."""
    if document.change_id != change_id:
        return _invalid_detail(
            FAILURE_ANALYSIS_SOURCE,
            f"change_id: expected {change_id!r}, found {document.change_id!r}",
        )
    if not authoritative_batch_id:
        return _invalid_detail(
            FAILURE_ANALYSIS_SOURCE,
            "the projection has no authoritative batch, so these failures cannot be attributed to one",
        )
    if document.batch_id == authoritative_batch_id and document.source_batch_id == authoritative_batch_id:
        return ""
    return _invalid_detail(
        FAILURE_ANALYSIS_SOURCE,
        f"batch identity: expected batch_id and source_batch_id {authoritative_batch_id!r}, "
        f"found batch_id={document.batch_id!r} source_batch_id={document.source_batch_id!r}",
    )


def _cycle_detail(chain: Sequence[str]) -> str:
    """Render an alias cycle from its lowest member, whichever end found it.

    The same cycle is reachable from every problem in it, so keying the gap on
    the entry point would report one gap per reference. Rotating to the minimum
    makes the rendering — and therefore the gap — identical for all of them.
    """
    start = chain.index(min(chain))
    rotated = [*chain[start:], *chain[:start]]
    return "merge alias cycle: " + " -> ".join([*rotated, rotated[0]])


def _resolve_canonical(
    source_id: str,
    problems: Mapping[str, ProblemRow],
    reported: set[str],
    fold: _Fold,
) -> ProblemRow | None:
    """Follow ``merged_into`` aliases to the canonical Problem (A7).

    Returns ``None`` when the chain cannot be resolved — a cycle, or a reference
    to a problem_id the ledger does not contain — and reports one
    ``problem_alias_invalid`` gap per distinct broken link rather than per
    referring case. An unresolved chain contributes no open problem *and* is
    never treated as closed: the gap is the only conclusion the fold may draw.

    These details describe a *logical reference error inside* a ledger the fold
    read and accepted, so they intentionally carry neither ``GAP_DETAIL_MISSING``
    nor ``GAP_DETAIL_INVALID``: those prefixes mean "this input document as a
    whole is unusable", and the ledger here is neither absent nor refused. The
    ``reported`` keys below are internal dedupe keys, not gap details.
    """
    chain: list[str] = []
    current = source_id
    while True:
        if current in chain:
            cycle = chain[chain.index(current) :]
            _report_alias_gap(f"cycle:{min(cycle)}", _cycle_detail(cycle), reported, fold)
            return None
        problem = problems.get(current)
        if problem is None:
            _report_alias_gap(
                f"absent-problem:{current}",
                f"{current!r} is referenced by the issue snapshot or a merge alias "
                "but is not in the problem ledger",
                reported,
                fold,
            )
            return None
        chain.append(current)
        target = problem.merge_target()
        if target is None:
            return problem
        current = target


def _report_alias_gap(key: str, detail: str, reported: set[str], fold: _Fold) -> None:
    if key in reported:
        return
    reported.add(key)
    fold.gap("problem_alias_invalid", PROJECT_PROBLEMS_SOURCE, detail=detail)


def _read_problem_facts(
    project_root: Path,
    change_dir: Path,
    change_id: str,
    fold: _Fold,
) -> Mapping[str, tuple[TraceProblemFact, ...]]:
    """The two-hop join: case_id → Observation → Occurrence → Problem (spec §9).

    Either input being unusable suppresses the whole join, which is why the
    alias resolution below cannot mistake an absent ledger for thousands of
    dangling references. A snapshot that reports its own analysis or project sync
    as incomplete counts as unusable for the same reason (see
    ``IssuesSnapshotDocument.degraded_reason``). Returns every canonical Problem
    each case reaches; ``_open_ids`` narrows that to the routing subset.
    """
    snapshot = _read_fold_document(
        change_dir / "issues" / "snapshot.json",
        ISSUES_SNAPSHOT_SOURCE,
        IssuesSnapshotDocument,
        code="issues_snapshot_missing",
        fold=fold,
    )
    if snapshot is not None:
        reason = (
            f"change_id: expected {change_id!r}, found {snapshot.change_id!r}"
            if snapshot.change_id != change_id
            else snapshot.degraded_reason()
        )
        if reason:
            fold.gap(
                "issues_snapshot_missing",
                ISSUES_SNAPSHOT_SOURCE,
                detail=_invalid_detail(ISSUES_SNAPSHOT_SOURCE, reason),
            )
            snapshot = None
    ledger = _read_fold_document(
        project_root / "qa" / "issues" / "problems.json",
        PROJECT_PROBLEMS_SOURCE,
        ProblemLedgerDocument,
        code="problems_snapshot_missing",
        fold=fold,
    )
    if snapshot is None or ledger is None:
        return {}

    # First declaration wins for a duplicated observation_id, mirroring
    # `_unique_entries`: two rows claiming one id is a contradictory snapshot,
    # and letting the last one win would make the join depend on list order.
    # A first declaration that names no case therefore joins nothing.
    case_of_observation: dict[str, str | None] = {}
    for observation in snapshot.observations:
        case_of_observation.setdefault(observation.observation_id, observation.case_id)

    referenced: dict[str, set[str]] = {}
    for occurrence in snapshot.occurrences:
        for observation_id in occurrence.observation_ids:
            case_id = case_of_observation.get(observation_id)
            if case_id:
                referenced.setdefault(case_id, set()).add(occurrence.problem_id)

    # First declaration wins here too: a ledger with two entries for one
    # problem_id is contradictory, and picking the later one would make the fold
    # depend on list order.
    problems: dict[str, ProblemRow] = {}
    for problem in ledger.problems:
        problems.setdefault(problem.problem_id, problem)

    # Resolve every referenced id once, in sorted order, so the gaps a broken
    # ledger produces do not depend on which case happened to reference it.
    reported: set[str] = set()
    canonical: dict[str, ProblemRow | None] = {
        source_id: _resolve_canonical(source_id, problems, reported, fold)
        for source_id in sorted({pid for ids in referenced.values() for pid in ids})
    }
    return {case_id: _problem_facts(sorted(ids), canonical) for case_id, ids in sorted(referenced.items())}


def _problem_facts(
    source_ids: Sequence[str],
    canonical: Mapping[str, ProblemRow | None],
) -> tuple[TraceProblemFact, ...]:
    """One fact per canonical Problem the case reaches, ordered by problem id.

    Aliases collapse: several referenced ids can resolve to the same canonical
    Problem, and all of them are kept in ``source_problem_ids`` so the trail back
    to the occurrence survives. A referenced id whose chain could not be resolved
    contributes no fact — there is no canonical Problem to describe, and the
    ``problem_alias_invalid`` gap is the only conclusion available.
    """
    aliases: dict[str, set[str]] = {}
    resolved: dict[str, ProblemRow] = {}
    for source_id in source_ids:
        problem = canonical[source_id]
        if problem is None:
            continue
        aliases.setdefault(problem.problem_id, set()).add(source_id)
        resolved[problem.problem_id] = problem
    return tuple(
        TraceProblemFact(
            problem_id=problem_id,
            source_problem_ids=tuple(sorted(aliases[problem_id])),
            fingerprint=resolved[problem_id].fingerprint.digest,
            status=resolved[problem_id].status,
            classification=resolved[problem_id].assessment.classification,
            open_product_bug=resolved[problem_id].is_open(),
        )
        for problem_id in sorted(resolved)
    )


def _open_ids(facts: Sequence[TraceProblemFact]) -> tuple[str, ...]:
    """The routing subset of one row's facts, deduplicated by fingerprint.

    Two Problems that share a fingerprint are one problem the ledger has not
    merged yet (spec §9.5), so the row routes the first by problem_id rather than
    counting it twice. The dedupe is per row and applies to routing only: both
    Problems keep their own fact, aliases included, so nothing is lost — a
    consumer that wants the un-deduplicated view reads ``problem_facts``.
    """
    seen_fingerprints: set[str] = set()
    open_ids: list[str] = []
    for fact in facts:
        if not fact.open_product_bug or fact.fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fact.fingerprint)
        open_ids.append(fact.problem_id)
    return tuple(open_ids)


def _enriched(
    row: TraceRow,
    failures: Mapping[str, tuple[TraceFailure, ...]],
    problem_facts: Mapping[str, tuple[TraceProblemFact, ...]],
) -> TraceRow:
    """The same row plus its reconciled facts.

    A copy of the execution-phase row, so every other field is carried over by
    construction — the dual-temporal invariant cannot be broken by a field this
    function has never heard of.
    """
    facts = problem_facts.get(row.case_id, ())
    return row.model_copy(
        update={
            "failures": failures.get(row.case_id, ()),
            "open_problem_ids": _open_ids(facts),
            "problem_facts": facts,
        }
    )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthoritativeInstant:
    """The instant a change's authoritative batch ran, and where it came from.

    Sufficiency is a question about a *cutoff*, so it needs an "as of". Taking
    that from the wall clock would make one batch's bytes sufficient today and
    stale tomorrow, so the same replay could route differently on every run;
    taking it from the batch makes the verdict a function of the evidence alone.

    ``ts_source`` is carried for the same reason ``TraceExecution`` carries it:
    ``batch_id_legacy_utc`` is an approximation (a batch id has no zone), and a
    consumer comparing two changes deserves to know which of them was pinned by
    an explicit ``executed_at``.
    """

    batch_id: str
    as_of: datetime
    ts_source: TsSource


def authoritative_batch_instant(project_root: Path, change_id: str) -> AuthoritativeInstant | None:
    """The instant to judge this change's evidence at, or ``None`` if there is none.

    ``None`` means the change names no orderable batch — no readable manifest, or
    one whose ``batch_id`` is neither timestamped nor accompanied by
    ``executed_at``. That is a refusal, not a default: a caller must record that
    nothing could be judged rather than substitute a clock, which is precisely
    the dependency this function exists to remove.

    Resolution matches ``_resolve_batch_ts`` for the current batch — explicit
    ``executed_at`` first, the legacy batch-id instant second — so the instant a
    verdict is judged at and the ``ts`` its rows carry cannot disagree.
    """
    change_dir = resolve_change(project_root, change_id).path
    view, _ = _read_manifest_view(change_dir / "execution" / "execution-manifest.yaml", change_id)
    if view is None:
        return None
    if view.executed_at is not None:
        return AuthoritativeInstant(view.batch_id, view.executed_at, "executed_at")
    legacy = _parse_batch_id(view.batch_id)
    if legacy is None:
        return None
    return AuthoritativeInstant(view.batch_id, legacy, "batch_id_legacy_utc")


def fold_trace(
    project_root: Path,
    change_id: str,
    *,
    phase: Literal["execution", "reconciled"] = "execution",
    current: ExecutionFoldInput | None = None,
) -> TraceProjection:
    """Fold one change's on-disk evidence into a fact projection.

    Pass ``current`` from the runner (pre-manifest); leave it ``None`` to read
    the published manifest. ``phase="reconciled"`` runs the identical fold and
    then adds the failure and open-problem facts (spec §9).
    """
    change_dir = resolve_change(project_root, change_id).path
    fold = _Fold()

    entries, case_gaps = load_case_entries(change_dir)
    fold.gaps.extend(case_gaps)
    _case_sources(change_dir, fold)

    view = _current_view(change_dir, change_id, current, fold)
    fold.sources.append(
        TraceSource(
            path=MANIFEST_FOLD_VIEW_SOURCE,
            exists=view is not None,
            sha256=view.digest() if view is not None else None,
        )
    )

    batches = _read_batches(change_dir, change_id, view, fold)
    _report_missing_current_results(view, change_dir, fold)

    tree = scan_test_tree(project_root)
    fold.sources.append(TraceSource(path=TESTS_TREE_SCAN_SOURCE, exists=True, sha256=tree.tree_digest))
    _report_tree_drift(view, tree, fold)

    # The scan lists its functions in a stable order; the fold only asks whether
    # a given one is still there, so index them once for the whole row loop.
    tree_functions = frozenset(tree.functions)
    rows: list[TraceRow] = []
    for entry in _unique_entries(entries):
        row = _build_row(entry, batches, view, tree)
        rows.append(row)
        _report_missing_mapped_tests(entry, row, batches, tree_functions, fold)

    authoritative_batch_id = view.batch_id if view is not None else ""
    if phase == "reconciled":
        failures = _read_failures(change_dir, change_id, authoritative_batch_id, fold)
        problem_facts = _read_problem_facts(project_root, change_dir, change_id, fold)
        rows = [_enriched(row, failures, problem_facts) for row in rows]

    authoritative = (
        next((batch for batch in batches if batch.batch_id == view.batch_id), None)
        if view is not None
        else None
    )
    return TraceProjection(
        schema_version="1",
        change_id=change_id,
        phase=phase,
        authoritative_batch_id=authoritative_batch_id,
        sources=tuple(sorted(fold.sources, key=lambda source: source.path)),
        rows=tuple(rows),
        unmapped_tests=authoritative.unmapped if authoritative is not None else (),
        gaps=tuple(fold.gaps),
        integrity=_integrity(rows, fold.gaps),
    )


__all__ = [
    "FAILURE_ANALYSIS_SOURCE",
    "GAP_DETAIL_INVALID",
    "GAP_DETAIL_MISSING",
    "ISSUES_SNAPSHOT_SOURCE",
    "MANIFEST_FOLD_VIEW_SOURCE",
    "PROJECT_PROBLEMS_SOURCE",
    "TESTS_TREE_SCAN_SOURCE",
    "AuthoritativeInstant",
    "ExecutionFoldInput",
    "FailureAnalysisDocument",
    "FailureRow",
    "IssuesSnapshotDocument",
    "ManifestFoldView",
    "PerformanceResultDocument",
    "PerformanceScenarioRow",
    "ProblemLedgerDocument",
    "ProblemRow",
    "ResultDocument",
    "ResultTestRow",
    "ResultUnmappedRow",
    "SnapshotAnalysisStatusRow",
    "SnapshotObservationRow",
    "SnapshotOccurrenceRow",
    "fold_trace",
]
