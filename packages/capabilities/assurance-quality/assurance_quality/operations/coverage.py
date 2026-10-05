"""Deterministic coverage, MRC, quarantine, C-layer, and applicability handlers."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict


from assurance_generation.contracts.families import LayerName
from assurance_quality.contracts.coverage import (
    CoverageGap,
    CoverageGapFeedstock,
    CoverageGapKind,
    CoverageGapLocator,
    CoverageGapsDocument,
    MinimumCoverageItem,
    MinimumCoverageMatrixRow,
    MinimumCoverageResult,
    MrcItemStatus,
)
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.goal_scope import has_layer_evidence

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_STALE_REASON_CODES = frozenset({"execution_stale", "pass_stale"})
_CASE_TYPES = frozenset({"API", "E2E", "Fuzz", "Performance"})
_LAYER_TO_CASE_TYPE: dict[str, str] = {
    "api": "API",
    "e2e": "E2E",
    "fuzz": "Fuzz",
    "performance": "Performance",
}


class MinimumCoverageInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    items: tuple[MinimumCoverageMatrixRow, ...]
    executed_case_ids: tuple[str, ...] = ()
    failed_case_ids: tuple[str, ...] = ()
    open_issue_case_ids: tuple[str, ...] = ()
    # Plan-bound assessments provide authenticated layers; historical callers
    # without layer metadata retain their original join interpretation.
    case_layers: dict[str, LayerName] | None = None


def projection_digest(projection: TraceProjectionV2) -> str:
    payload = projection.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _insufficient_reasons(sufficiency: TraceSufficiencyFacts | None) -> Mapping[str, frozenset[str]]:
    if sufficiency is None or sufficiency.error_code is not None:
        return {}
    return {item.case_id: frozenset(item.reason_codes) for item in sufficiency.insufficient_cases}


def _gap(
    *,
    kind: CoverageGapKind,
    locator: CoverageGapLocator,
    batch_id: str,
    digest: str,
    layer: Literal["execution", "declaration"] = "execution",
) -> CoverageGap:
    return CoverageGap(
        kind=kind,
        locator=locator,
        layer=layer,
        batch_id=batch_id,
        evidence_refs=(digest,),
    )


def build_coverage_gaps(
    projection: TraceProjectionV2,
    sufficiency: TraceSufficiencyFacts | None,
    *,
    change_id: str,
    batch_id: str,
    feedstock: CoverageGapFeedstock | None = None,
) -> CoverageGapsDocument:
    digest = projection_digest(projection)
    gaps: list[CoverageGap] = []
    rows = {row.case_id: row for row in projection.rows}
    reasons = _insufficient_reasons(sufficiency)
    uncovered_required: set[str] = set()
    for case_id, row in rows.items():
        if row.automation_required and row.coverage_state == "uncovered":
            uncovered_required.add(case_id)
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
        if row is None or not row.automation_required or row.coverage_state == "uncovered":
            continue
        if codes & _STALE_REASON_CODES:
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
        if item.file.strip():
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
    for case_id in ext.cases_without_strong_oracle:
        if case_id not in uncovered_required:
            gaps.append(
                _gap(
                    kind="uncovered_required_case",
                    locator=CoverageGapLocator(case_id=case_id),
                    batch_id=batch_id,
                    digest=digest,
                )
            )
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


def join_minimum_coverage(payload: MinimumCoverageInput) -> MinimumCoverageResult:
    executed = set(payload.executed_case_ids)
    failed = set(payload.failed_case_ids)
    known = set(payload.open_issue_case_ids)
    items: list[MinimumCoverageItem] = []
    for row in payload.items:
        status: MrcItemStatus
        mapped = set(row.covered_by_cases)

        def covers_layer(case_ids: set[str]) -> bool:
            if payload.case_layers is None:
                return bool(case_ids)
            return has_layer_evidence(row.layer, case_ids, payload.case_layers)

        if row.status == "skipped_by_scope" or not row.required:
            status = "skipped_by_scope"
        elif not covers_layer(mapped):
            status = "missing"
        elif not covers_layer(mapped & executed):
            status = "not_executed"
        elif set(row.covered_by_cases) & failed and set(row.covered_by_cases) & known:
            status = "covered_known_issue"
        elif set(row.covered_by_cases) & failed:
            status = "covered_but_failing"
        else:
            status = "covered"
        items.append(
            MinimumCoverageItem(
                mrc_id=row.mrc_id,
                key=row.key,
                proposed_key=row.proposed_key,
                category=row.category,
                required=row.required,
                layer=row.layer,
                status=status,
                case_ids=tuple(row.covered_by_cases),
                executed_case_ids=tuple(case_id for case_id in row.covered_by_cases if case_id in executed),
            )
        )
    return MinimumCoverageResult.of(change_id=payload.change_id, items=items)


_REPAIRABLE_GAP_KINDS: frozenset[str] = frozenset(
    {
        "uncovered_required_case",
        "stale_required_case",
        "constraint_without_property",
        "matrix_cell_unasserted",
    }
)
_OBLIGATION_GAP_KINDS = frozenset(
    {
        "obligation_case_missing",
        "obligation_mapping_missing",
        "obligation_observation_missing",
    }
)
_GAP_METRICS: dict[str, str] = {
    "uncovered_required_case": "coverage",
    "stale_required_case": "coverage",
    "constraint_without_property": "constraint_coverage",
    "matrix_cell_unasserted": "auth_matrix",
}
