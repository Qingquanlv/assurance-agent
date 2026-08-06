"""Pure fold: TraceProjection + sufficiency → typed coverage-gap signals.

Zero LLM. Deterministic. Not an M4 MetricKey aggregator — C2 feedstock helpers
(``count_closed_gaps`` / ``diff_coverage_gaps``) compare gap identities only.

Phase-1 active kinds (always considered from current wire facts):
  - ``uncovered_required_case`` — projection ``coverage_state == uncovered``
  - ``stale_required_case`` — sufficiency ``execution_stale`` / ``pass_stale``
  - ``unmapped_test_cluster`` — projection ``unmapped_tests`` grouped by file

Phase-1 extension kinds (emit only when feedstock lists are non-empty):
  - ``constraint_without_property``
  - ``matrix_cell_unasserted``

TraceProjection has no constraint/matrix gap fields today; those kinds stay
stub-empty unless a caller passes ``CoverageGapFeedstock``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    CoverageGap,
    CoverageGapFeedstock,
    CoverageGapKind,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.trace import TraceProjection, TraceRow
from assurance_agent.artifacts.models.trace_sufficiency import TraceSufficiencyFacts

# Documented Phase-1 split (pinned by tests).
PHASE1_ACTIVE_KINDS: frozenset[CoverageGapKind] = frozenset(
    {
        "uncovered_required_case",
        "stale_required_case",
        "unmapped_test_cluster",
    }
)
PHASE1_EXTENSION_KINDS: frozenset[CoverageGapKind] = frozenset(
    {
        "constraint_without_property",
        "matrix_cell_unasserted",
    }
)

_STALE_REASON_CODES = frozenset({"execution_stale", "pass_stale"})

GapIdentity = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class CoverageGapDiff:
    """Closed / opened / reopened gap identities between two documents.

    Pure feedstock for a future C2 gap-closure rate — **not** a MetricKey.
    """

    closed: tuple[GapIdentity, ...]
    opened: tuple[GapIdentity, ...]
    reopened: tuple[GapIdentity, ...]


def projection_digest(projection: TraceProjection) -> str:
    """Stable digest of the projection bytes this fold consumed."""
    return sha256_bytes(canonical_json_bytes(projection))


def gap_identity(gap: CoverageGap) -> GapIdentity:
    """Fingerprint key for cross-batch diff (kind + locator axes)."""
    loc = gap.locator
    return (
        gap.kind,
        loc.case_id or "",
        loc.constraint_key or "",
        loc.cell or "",
        loc.cluster_key or "",
    )


def _rows_by_case(projection: TraceProjection) -> dict[str, TraceRow]:
    return {row.case_id: row for row in projection.rows}


def _insufficient_reason_map(sufficiency: object | None) -> Mapping[str, frozenset[str]]:
    """Extract case_id → reason_codes from TraceSufficiencyFacts or SufficiencyReport.

    Unjudged facts (``error_code`` set) contribute nothing — never invent stale.
    """
    if sufficiency is None:
        return {}
    error_code = getattr(sufficiency, "error_code", None)
    if error_code is not None:
        return {}

    out: dict[str, frozenset[str]] = {}
    # TraceSufficiencyFacts.insufficient_cases
    cases = getattr(sufficiency, "insufficient_cases", None)
    if cases is not None:
        for item in cases:
            out[item.case_id] = frozenset(item.reason_codes)
        return out

    # SufficiencyReport.insufficient_rows (RowVerdict)
    rows = getattr(sufficiency, "insufficient_rows", None)
    if rows is not None:
        for item in rows:
            out[item.case_id] = frozenset(item.reason_codes)
        return out

    # SufficiencyReport.rows fallback
    all_rows = getattr(sufficiency, "rows", None)
    if isinstance(all_rows, Sequence):
        for item in all_rows:
            if getattr(item, "sufficient", True):
                continue
            out[item.case_id] = frozenset(getattr(item, "reason_codes", ()))
    return out


def _gap(
    *,
    kind: CoverageGapKind,
    locator: CoverageGapLocator,
    batch_id: str,
    digest: str,
    layer: str = "execution",
) -> CoverageGap:
    return CoverageGap(
        kind=kind,
        locator=locator,
        layer=layer,  # type: ignore[arg-type]
        batch_id=batch_id,
        evidence_refs=(digest,),
    )


def build_coverage_gaps(
    projection: TraceProjection,
    sufficiency: TraceSufficiencyFacts | Any | None,
    *,
    change_id: str,
    batch_id: str,
    feedstock: CoverageGapFeedstock | None = None,
) -> CoverageGapsDocument:
    """Fold projection (+ optional sufficiency / feedstock) into typed gaps.

    Sort is applied by ``CoverageGapsDocument`` validation (kind order, then
    locator axes). Extension kinds are omitted unless ``feedstock`` lists them.
    """
    digest = projection_digest(projection)
    gaps: list[CoverageGap] = []
    rows = _rows_by_case(projection)
    reasons = _insufficient_reason_map(sufficiency)

    for case_id, row in rows.items():
        if row.automation_required and row.coverage_state == "uncovered":
            gaps.append(
                _gap(
                    kind="uncovered_required_case",
                    locator=CoverageGapLocator(case_id=case_id),
                    batch_id=batch_id,
                    digest=digest,
                )
            )

    for case_id, codes in reasons.items():
        row = rows.get(case_id)
        if row is None or not row.automation_required:
            continue
        if not codes & _STALE_REASON_CODES:
            continue
        # Uncovered already emitted above; stale is for evidence age on a mapped case.
        if row.coverage_state == "uncovered":
            continue
        gaps.append(
            _gap(
                kind="stale_required_case",
                locator=CoverageGapLocator(case_id=case_id),
                batch_id=batch_id,
                digest=digest,
            )
        )

    by_file: dict[str, list[str]] = defaultdict(list)
    for item in projection.unmapped_tests:
        by_file[item.file].append(item.test_name)
    for file_path in by_file:
        gaps.append(
            _gap(
                kind="unmapped_test_cluster",
                locator=CoverageGapLocator(cluster_key=file_path),
                batch_id=batch_id,
                digest=digest,
            )
        )

    ext = feedstock or CoverageGapFeedstock()
    for key in ext.constraints_without_property:
        gaps.append(
            _gap(
                kind="constraint_without_property",
                locator=CoverageGapLocator(constraint_key=key),
                batch_id=batch_id,
                digest=digest,
            )
        )
    for cell in ext.matrix_cells_unasserted:
        gaps.append(
            _gap(
                kind="matrix_cell_unasserted",
                locator=CoverageGapLocator(cell=cell),
                batch_id=batch_id,
                digest=digest,
            )
        )

    return CoverageGapsDocument(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        projection_digest=digest,
        gaps=tuple(gaps),
        computed_at=None,
    )


def _identity_set(doc: CoverageGapsDocument) -> set[GapIdentity]:
    return {gap_identity(gap) for gap in doc.gaps}


def diff_coverage_gaps(
    previous: CoverageGapsDocument,
    current: CoverageGapsDocument,
    *,
    historically_closed: Iterable[GapIdentity] = (),
) -> CoverageGapDiff:
    """Diff gap identities across batches (C2 feedstock, not MetricKey).

    ``reopened`` = present in ``current``, absent in ``previous``, and listed in
    ``historically_closed``. Without history, reopened stays empty.
    """
    prev = _identity_set(previous)
    cur = _identity_set(current)
    closed = tuple(sorted(prev - cur))
    opened_raw = cur - prev
    hist = frozenset(historically_closed)
    reopened = tuple(sorted(opened_raw & hist))
    opened = tuple(sorted(opened_raw - hist))
    return CoverageGapDiff(closed=closed, opened=opened, reopened=reopened)


def count_closed_gaps(previous: CoverageGapsDocument, current: CoverageGapsDocument) -> int:
    """How many prior gap identities are absent from ``current``."""
    return len(diff_coverage_gaps(previous, current).closed)


__all__ = [
    "PHASE1_ACTIVE_KINDS",
    "PHASE1_EXTENSION_KINDS",
    "CoverageGapDiff",
    "GapIdentity",
    "build_coverage_gaps",
    "count_closed_gaps",
    "diff_coverage_gaps",
    "gap_identity",
    "projection_digest",
]
