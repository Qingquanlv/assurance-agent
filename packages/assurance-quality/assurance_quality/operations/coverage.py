"""Deterministic coverage, MRC, quarantine, C-layer, and applicability handlers."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.plans import LayerApplicability
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
from assurance_quality.contracts.c_layer import CLayerMetricEntry, CLayerMetricsDocument
from assurance_quality.contracts.pr_metrics import (
    AuthMatrixCellResult,
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    CoverageDiffFile,
    JourneyCoverageEvidence,
    JourneyCoverageItem,
    PerfSlackEvidence,
    PerfSlackScenario,
)
from assurance_quality.contracts.quarantine import QuarantineEntry, QuarantineProjection
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.trace import TraceProjectionV2, TraceRow
from assurance_quality.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    json_digest,
    scope_of,
    succeeded,
    validate_input,
)
from assurance_quality.operations.trace import TraceOperationInput, project_trace

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_STALE_REASON_CODES = frozenset({"execution_stale", "pass_stale"})
_CASE_TYPES = frozenset({"API", "E2E", "Fuzz", "Performance"})
_LAYER_TO_CASE_TYPE: dict[str, str] = {
    "api": "API",
    "e2e": "E2E",
    "fuzz": "Fuzz",
    "performance": "Performance",
}


class CoverageGapsInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    projection: dict[str, Any]
    capability_leafs: tuple[str, ...]
    sufficiency: dict[str, Any] | None = None
    feedstock: CoverageGapFeedstock | None = None


class MinimumCoverageInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    items: tuple[MinimumCoverageMatrixRow, ...]
    executed_case_ids: tuple[str, ...] = ()
    failed_case_ids: tuple[str, ...] = ()
    open_issue_case_ids: tuple[str, ...] = ()


class DiffCoverageInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    files: tuple[CoverageDiffFile, ...]
    source: dict[str, str] = Field(default_factory=dict)


class ConstraintCoverageInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    declared_keys: tuple[str, ...]
    covered_keys: tuple[str, ...]
    touched_keys: tuple[str, ...] = ()
    source_digest: str


class AuthMatrixInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    cells: tuple[AuthMatrixCellResult, ...]
    source_digest: str


class JourneyCoverageInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    items: tuple[JourneyCoverageItem, ...]
    source_digest: str


class ThresholdSlackInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    scenarios: tuple[PerfSlackScenario, ...]
    slack_band: float = 10.0
    source_digest: str


class QuarantineInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    entries: tuple[QuarantineEntry, ...] = ()


class CLayerInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    computed_at: datetime
    escape_rate: CLayerMetricEntry
    counterexample_promotion_rate: CLayerMetricEntry
    coverage_gap_closure_rate: CLayerMetricEntry
    seed_replay_stability: CLayerMetricEntry


class CoverageRepairNeedInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    gaps: CoverageGapsDocument
    journey: JourneyCoverageEvidence | None = None


class LayerApplicabilityInput(BaseModel):
    model_config = _FROZEN

    layer: LayerName
    cases: tuple[dict[str, Any], ...]


class CombinedTraceGapsInput(TraceOperationInput):
    sufficiency: dict[str, Any] | None = None
    feedstock: CoverageGapFeedstock | None = None


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
        if row.status == "skipped_by_scope" or not row.required:
            status = "skipped_by_scope"
        elif not row.covered_by_cases:
            status = "missing"
        elif not set(row.covered_by_cases) & executed:
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
                category=row.category or "api",
                required=row.required,
                layer=row.layer or "api",
                status=status,
                case_ids=tuple(row.covered_by_cases),
                executed_case_ids=tuple(case_id for case_id in row.covered_by_cases if case_id in executed),
            )
        )
    return MinimumCoverageResult.of(change_id=payload.change_id, items=items)


def collect_diff_coverage(payload: DiffCoverageInput) -> CoverageDiffEvidence:
    total = sum(len(item.changed_lines) for item in payload.files)
    covered = sum(len(item.covered_lines) for item in payload.files)
    return CoverageDiffEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        total_changed_lines=total,
        covered_changed_lines=covered,
        value=None if total == 0 else covered / total,
        files=payload.files,
        source=dict(payload.source),
    )


def compute_constraint_coverage(payload: ConstraintCoverageInput) -> ConstraintCoverageEvidence:
    declared_keys = tuple(sorted(set(payload.declared_keys)))
    covered = frozenset(payload.covered_keys)
    touched = tuple(sorted(set(payload.touched_keys or declared_keys)))
    declared = scope_of(
        total=len(declared_keys),
        covered=sum(1 for key in declared_keys if key in covered),
        uncovered=tuple(key for key in declared_keys if key not in covered),
    )
    touched_scope = scope_of(
        total=len(touched),
        covered=sum(1 for key in touched if key in covered),
        uncovered=tuple(key for key in touched if key not in covered),
    )
    return ConstraintCoverageEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        declared=declared,
        touched=touched_scope,
        value=declared.value,
    )


def compute_auth_matrix(payload: AuthMatrixInput) -> AuthMatrixEvidence:
    cells = tuple(sorted(payload.cells, key=lambda cell: cell.cell_id))
    declared = scope_of(
        total=len(cells),
        covered=sum(1 for cell in cells if cell.asserted),
        uncovered=tuple(cell.cell_id for cell in cells if not cell.asserted),
    )
    return AuthMatrixEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        declared=declared,
        touched=declared,
        value=declared.value,
        cells=cells,
    )


def compute_journey_coverage(payload: JourneyCoverageInput) -> JourneyCoverageEvidence:
    items = tuple(sorted(payload.items, key=lambda item: item.journey_key))
    covered = tuple(item.journey_key for item in items if item.covered and not item.quarantined)
    declared = scope_of(
        total=len(items),
        covered=len(covered),
        uncovered=tuple(item.journey_key for item in items if item.journey_key not in covered),
    )
    return JourneyCoverageEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        declared=declared,
        touched=declared,
        value=declared.value,
        items=items,
    )


def compute_threshold_slack(payload: ThresholdSlackInput) -> PerfSlackEvidence:
    slacks = tuple(item.slack for item in payload.scenarios if item.slack is not None)
    return PerfSlackEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        value=max(slacks) if slacks else None,
        slack_band=payload.slack_band,
        scenarios=payload.scenarios,
    )


def derive_layer_applicability(
    cases: Sequence[Mapping[str, object]],
    layer: LayerName,
) -> LayerApplicability:
    if layer not in LAYER_NAMES:
        raise InputError(f"unknown generation family: {layer}")
    expected = _LAYER_TO_CASE_TYPE[layer]
    case_ids: set[str] = set()
    for document_index, document in enumerate(cases):
        if not isinstance(document, Mapping):
            raise InputError(f"cases[{document_index}] must be a mapping")
        for bucket in ("added", "modified"):
            if bucket not in document:
                raise InputError(f"cases[{document_index}] missing required key '{bucket}'")
            entries = document[bucket]
            if not isinstance(entries, list):
                raise InputError(f"cases[{document_index}].{bucket} must be a list")
            for entry_index, entry in enumerate(entries):
                locator = f"cases[{document_index}].{bucket}[{entry_index}]"
                if not isinstance(entry, Mapping):
                    raise InputError(f"{locator} must be a mapping")
                case_type = entry.get("type")
                if case_type not in _CASE_TYPES:
                    raise InputError(f"{locator}.type is invalid")
                automation = entry.get("automation") or {}
                if automation and not isinstance(automation, Mapping):
                    raise InputError(f"{locator}.automation must be a mapping")
                required = (
                    bool(automation.get("required", False)) if isinstance(automation, Mapping) else False
                )
                if required and case_type == expected:
                    case_id = entry.get("case_id")
                    if not isinstance(case_id, str) or not case_id.strip():
                        raise InputError(f"{locator}.case_id must be a non-empty string")
                    case_ids.add(case_id)
    ordered = tuple(sorted(case_ids))
    return LayerApplicability(
        layer=layer,
        applicable=bool(ordered),
        reason_code="automated_cases_present" if ordered else "no_automated_cases",
        case_ids=ordered,
    )


def probe_coverage_repair_need(payload: CoverageRepairNeedInput) -> dict[str, object]:
    repairable = [
        gap.model_dump(mode="json")
        for gap in payload.gaps.gaps
        if gap.kind in {"uncovered_required_case", "constraint_without_property", "matrix_cell_unasserted"}
    ]
    weak: list[str] = []
    if payload.journey is not None:
        weak.extend(
            case_id
            for item in payload.journey.items
            if item.status in {"weak_oracle", "oracle_unavailable"}
            for case_id in item.case_ids
        )
    return {
        "change_id": payload.change_id,
        "batch_id": payload.batch_id,
        "needed": bool(repairable or weak),
        "items": repairable,
        "weak_oracle_case_ids": list(sorted(set(weak))),
        "gaps_digest": json_digest(payload.gaps.model_dump(mode="json")),
    }


def _load_projection(raw: dict[str, Any], leafs: tuple[str, ...]) -> TraceProjectionV2:
    try:
        return TraceProjectionV2.model_validate(raw, context={"capability_leafs": frozenset(leafs)})
    except ValidationError as error:
        raise InputError(str(error)) from error


def _load_sufficiency(raw: dict[str, Any] | None) -> TraceSufficiencyFacts | None:
    if raw is None:
        return None
    try:
        return TraceSufficiencyFacts.model_validate(raw)
    except ValidationError as error:
        raise InputError(str(error)) from error


class _Handler:
    input_model: type[BaseModel]
    builder: Any

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(self.input_model, request.input)
            result = self.builder(payload)
            if hasattr(result, "model_dump"):
                return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
            return succeeded(result)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)


class BuildCoverageGapsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(CoverageGapsInput, request.input)
            projection = _load_projection(payload.projection, payload.capability_leafs)
            gaps = build_coverage_gaps(
                projection,
                _load_sufficiency(payload.sufficiency),
                change_id=payload.change_id,
                batch_id=payload.batch_id,
                feedstock=payload.feedstock,
            )
            return TaskOutcome.succeeded(cast(JSONValue, gaps.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


class MaterializeTraceAndCoverageGapsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(CombinedTraceGapsInput, request.input)
            projection = project_trace(payload)
            gaps = build_coverage_gaps(
                projection,
                _load_sufficiency(payload.sufficiency),
                change_id=payload.change_id,
                batch_id=payload.batch_id,
                feedstock=payload.feedstock,
            )
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {"trace": projection.model_dump(mode="json"), "gaps": gaps.model_dump(mode="json")},
                )
            )
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)


class MaterializeMinimumCoverageHandler(_Handler):
    input_model = MinimumCoverageInput
    builder = staticmethod(join_minimum_coverage)


class CollectDiffCoverageHandler(_Handler):
    input_model = DiffCoverageInput
    builder = staticmethod(collect_diff_coverage)


class ComputeConstraintCoverageHandler(_Handler):
    input_model = ConstraintCoverageInput
    builder = staticmethod(compute_constraint_coverage)


class ComputeAuthMatrixHandler(_Handler):
    input_model = AuthMatrixInput
    builder = staticmethod(compute_auth_matrix)


class ComputeJourneyCoverageHandler(_Handler):
    input_model = JourneyCoverageInput
    builder = staticmethod(compute_journey_coverage)


class ComputeThresholdSlackHandler(_Handler):
    input_model = ThresholdSlackInput
    builder = staticmethod(compute_threshold_slack)


class MaterializeQuarantineProjectionHandler(_Handler):
    input_model = QuarantineInput

    @staticmethod
    def builder(payload: QuarantineInput) -> QuarantineProjection:
        return QuarantineProjection(schema_version="1", change_id=payload.change_id, entries=payload.entries)


class MaterializeCLayerMetricsHandler(_Handler):
    input_model = CLayerInput

    @staticmethod
    def builder(payload: CLayerInput) -> CLayerMetricsDocument:
        return CLayerMetricsDocument(
            schema_version="1",
            change_id=payload.change_id,
            cadence="report",
            computed_at=payload.computed_at,
            escape_rate=payload.escape_rate,
            counterexample_promotion_rate=payload.counterexample_promotion_rate,
            coverage_gap_closure_rate=payload.coverage_gap_closure_rate,
            seed_replay_stability=payload.seed_replay_stability,
        )


class ProbeCoverageRepairNeedHandler(_Handler):
    input_model = CoverageRepairNeedInput
    builder = staticmethod(probe_coverage_repair_need)


class DerivePlanLayerApplicabilityHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(LayerApplicabilityInput, request.input)
            result = derive_layer_applicability(payload.cases, payload.layer)
            return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


def uncovered_required_case_ids(projection: TraceProjectionV2) -> tuple[str, ...]:
    return tuple(
        row.case_id
        for row in projection.rows
        if row.automation_required and row.coverage_state == "uncovered"
    )


def row_by_case(projection: TraceProjectionV2, case_id: str) -> TraceRow | None:
    for row in projection.rows:
        if row.case_id == case_id:
            return row
    return None


def default_c_layer_entry() -> CLayerMetricEntry:
    return CLayerMetricEntry(status="not_evaluated", numerator=None, denominator=None, rate=None)


def report_now() -> datetime:
    return datetime(2026, 8, 22, tzinfo=UTC)
