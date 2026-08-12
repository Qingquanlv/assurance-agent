"""``operation:materialize-minimum-coverage`` — deterministic MRC × execution join.

Pure join lives here; ``evidence/`` stays free of collection I/O. The operation
reads the case-authored matrix + advisory category maps + published trace
projection + L1 data-knowledge, then writes ``report/minimum-coverage-result.json``.

Missing-input policy (aligned with empty/missing matrix):
- missing/empty matrix → succeed, ``written: false``, reason ``no_mrc_matrix``
- missing whole projection → succeed, ``written: false``, reason ``no_trace_projection``
- per-case missing execution → item status ``not_executed`` (join continues)

Graph wiring into the assurance path is deferred (Task 8).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.minimum_coverage import (
    MinimumCoverageItem,
    MinimumCoverageMatrix,
    MinimumCoverageMatrixRow,
    MinimumCoverageResult,
    MrcCategory,
    MrcFinding,
    MrcItemStatus,
    MrcLayer,
    MrcObligation,
    auth_known_keys,
    journey_known_keys,
    maps_from_advisory_mrc,
    mrc_closed_key_findings,
)
from assurance_agent.artifacts.models.trace import TraceProjection, TraceProjectionLike, TraceRow
from assurance_agent.knowledge.extract_constraints import constraint_known_keys
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace

MINIMUM_COVERAGE_MATRIX_REL = "trace/minimum-coverage-matrix.yaml"
MINIMUM_COVERAGE_RESULT_REL = "report/minimum-coverage-result.json"
TRACE_PROJECTION_REL = "inspect/trace-projection.json"
DATA_KNOWLEDGE_REL = ".aa/data-knowledge.yaml"
_ADVISORY_CANDIDATES = (
    "explore/advisory.json",
    "risk-advisory/advisory.json",
)

_CATEGORY_DEFAULT_LAYER: Mapping[MrcCategory, MrcLayer] = {
    "api": "api",
    "negative": "api",
    "data_integrity": "api",
    "e2e": "e2e",
    "e2e_if_enabled": "e2e",
}


@dataclass(frozen=True)
class LiftedObligations:
    """Matrix rows lifted to join obligations, plus typed mapping findings."""

    obligations: tuple[MrcObligation, ...]
    findings: tuple[MrcFinding, ...]


@dataclass(frozen=True)
class CaseExecutionJoin:
    """Shared case × execution join facts for MRC and PR journey collectors."""

    executed_case_ids: tuple[str, ...]
    statuses: tuple[str, ...]
    known_issue: bool = False


def projection_rows_by_case_id(projection: TraceProjectionLike) -> dict[str, TraceRow]:
    """Last-wins index of projection rows by ``case_id``."""
    return {row.case_id: row for row in projection.rows}


def execution_counts_for_current_batch(row: TraceRow, batch_id: str) -> bool:
    """Whether ``row.latest_execution`` is fresh for ``batch_id``.

    Counts when ``presence_in_current_batch == "executed"`` **or** the latest
    execution's ``batch_id`` matches. A stale ``latest_execution`` from another
    batch with presence ``not_in_current_batch`` does **not** count.
    """
    if row.latest_execution is None:
        return False
    if row.presence_in_current_batch == "executed":
        return True
    return row.latest_execution.batch_id == batch_id


def collect_case_executions(
    case_ids: Sequence[str],
    rows_by_id: Mapping[str, TraceRow],
    *,
    batch_id: str | None = None,
) -> CaseExecutionJoin:
    """Collect executed cases from a projection index.

    When ``batch_id`` is provided, only current-batch-fresh executions count
    (A4 / PR collectors). When omitted, any ``latest_execution`` counts — the
    Task 5 MRC shadow join keeps that broader rule for must_compat parity.
    """
    executed: list[str] = []
    statuses: list[str] = []
    known_issue = False
    for case_id in case_ids:
        row = rows_by_id.get(case_id)
        if row is None or row.latest_execution is None:
            continue
        if batch_id is not None and not execution_counts_for_current_batch(row, batch_id):
            continue
        executed.append(case_id)
        statuses.append(row.latest_execution.status)
        if row.latest_execution.status == "passed" and row.open_problem_ids:
            known_issue = True
    return CaseExecutionJoin(
        executed_case_ids=tuple(executed),
        statuses=tuple(statuses),
        known_issue=known_issue,
    )


def materialize_minimum_coverage(
    *,
    change_id: str,
    obligations: Sequence[MrcObligation],
    projection: TraceProjection,
    findings: Sequence[MrcFinding] = (),
) -> MinimumCoverageResult:
    """Join MRC obligations with a trace projection into a result document.

    Duplicate ``case_id`` rows in ``projection.rows`` resolve **last-wins** when
    building the join index (same rule as a dict comprehension over the row
    list). Obligation ``case_ids`` order is preserved; a case listed twice may
    therefore appear twice in ``executed_case_ids`` if both slots resolve.
    """
    rows_by_id = projection_rows_by_case_id(projection)
    items = tuple(_join_obligation(obligation, rows_by_id) for obligation in obligations)
    return MinimumCoverageResult.of(change_id=change_id, items=items, findings=findings)


def _join_obligation(
    obligation: MrcObligation,
    rows_by_id: Mapping[str, TraceRow],
) -> MinimumCoverageItem:
    if obligation.skipped_by_scope:
        return MinimumCoverageItem(
            mrc_id=obligation.mrc_id,
            key=obligation.key,
            category=obligation.category,
            required=obligation.required,
            layer=obligation.layer,
            status="skipped_by_scope",
            case_ids=obligation.case_ids,
            executed_case_ids=(),
            mapping_source=obligation.mapping_source,
        )
    if not obligation.case_ids:
        return MinimumCoverageItem(
            mrc_id=obligation.mrc_id,
            key=obligation.key,
            category=obligation.category,
            required=obligation.required,
            layer=obligation.layer,
            status="missing",
            case_ids=(),
            executed_case_ids=(),
            mapping_source=obligation.mapping_source,
        )

    # MRC shadow join: no batch freshness filter (Task 5 must_compat).
    joined = collect_case_executions(obligation.case_ids, rows_by_id, batch_id=None)
    status = _status_from_execution(
        joined.executed_case_ids,
        joined.statuses,
        known_issue=joined.known_issue,
    )
    return MinimumCoverageItem(
        mrc_id=obligation.mrc_id,
        key=obligation.key,
        category=obligation.category,
        required=obligation.required,
        layer=obligation.layer,
        status=status,
        case_ids=obligation.case_ids,
        executed_case_ids=joined.executed_case_ids,
        mapping_source=obligation.mapping_source,
    )


def _status_from_execution(
    executed: Sequence[str],
    statuses: Sequence[str],
    *,
    known_issue: bool,
) -> MrcItemStatus:
    if not executed:
        return "not_executed"
    if any(status == "failed" for status in statuses):
        return "covered_but_failing"
    if known_issue:
        return "covered_known_issue"
    return "covered"


def obligations_from_matrix(
    rows: Sequence[MinimumCoverageMatrixRow],
    *,
    category_by_key: Mapping[str, MrcCategory] | None = None,
    layer_by_key: Mapping[str, MrcLayer] | None = None,
    mrc_id_by_key: Mapping[str, str] | None = None,
) -> LiftedObligations:
    """Lift matrix rows into join obligations.

    Category/layer/mrc_id come from the row when set, else from advisory maps.
    Missing category after maps is **fail-closed for that row**: it is omitted
    from obligations and emitted as ``mrc_category_unresolved`` — never silently
    defaulted to ``api`` (which would falsify closed-key categories).
    """
    cat_map = category_by_key or {}
    layer_map = layer_by_key or {}
    id_map = mrc_id_by_key or {}
    out: list[MrcObligation] = []
    findings: list[MrcFinding] = []
    for row in rows:
        category = row.category or cat_map.get(row.key)
        if category is None:
            findings.append(
                MrcFinding(
                    code="mrc_category_unresolved",
                    key=row.key,
                    detail="matrix row lacks category and no advisory map entry",
                )
            )
            continue
        layer = row.layer or layer_map.get(row.key) or _CATEGORY_DEFAULT_LAYER[category]
        mrc_id = id_map.get(row.key) or row.mrc_id
        out.append(
            MrcObligation(
                mrc_id=mrc_id,
                key=row.key,
                category=category,
                required=row.required,
                layer=layer,
                case_ids=tuple(row.covered_by_cases),
                skipped_by_scope=row.status == "skipped_by_scope",
                skip_reason=row.skip_reason,
            )
        )
    return LiftedObligations(obligations=tuple(out), findings=tuple(findings))


def shadow_compare_minimum_coverage(
    actual: MinimumCoverageResult,
    legacy: Mapping[str, Any],
    *,
    compare_findings: bool = False,
) -> list[str]:
    """Field-level diffs against a historical skill-written result JSON.

    By default ``findings`` is excluded: the skill never emitted §12.12 findings;
    the deterministic writer may. Set ``compare_findings=True`` to include them.
    """
    diffs: list[str] = []
    if actual.change_id != legacy.get("change_id"):
        diffs.append(f"change_id: {actual.change_id!r} != {legacy.get('change_id')!r}")
    legacy_summary = legacy.get("summary") or {}
    for field in (
        "total_required",
        "covered",
        "covered_known_issue",
        "covered_but_failing",
        "not_executed",
        "missing",
        "skipped_by_scope",
    ):
        if getattr(actual.summary, field) != legacy_summary.get(field):
            diffs.append(
                f"summary.{field}: {getattr(actual.summary, field)!r} != {legacy_summary.get(field)!r}"
            )
    legacy_items = {item["mrc_id"]: item for item in legacy.get("items") or []}
    actual_items = {item.mrc_id: item for item in actual.items}
    if set(legacy_items) != set(actual_items):
        diffs.append(f"item mrc_ids: {sorted(actual_items)} != {sorted(legacy_items)}")
        return diffs
    for mrc_id, item in actual_items.items():
        legacy_item = legacy_items[mrc_id]
        for field in ("key", "category", "required", "layer", "status", "mapping_source"):
            if getattr(item, field) != legacy_item.get(field):
                diffs.append(f"{mrc_id}.{field}: {getattr(item, field)!r} != {legacy_item.get(field)!r}")
        if list(item.case_ids) != list(legacy_item.get("case_ids") or []):
            diffs.append(f"{mrc_id}.case_ids: {list(item.case_ids)} != {legacy_item.get('case_ids')}")
        if list(item.executed_case_ids) != list(legacy_item.get("executed_case_ids") or []):
            diffs.append(
                f"{mrc_id}.executed_case_ids: {list(item.executed_case_ids)} != "
                f"{legacy_item.get('executed_case_ids')}"
            )
    if compare_findings:
        legacy_findings = legacy.get("findings") or []
        actual_dump = [f.model_dump(mode="json") for f in actual.findings]
        if actual_dump != list(legacy_findings):
            diffs.append(f"findings: {actual_dump!r} != {legacy_findings!r}")
    return diffs


def _load_matrix(change_dir: Path) -> MinimumCoverageMatrix | None:
    path = change_dir / MINIMUM_COVERAGE_MATRIX_REL
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return MinimumCoverageMatrix.model_validate([])
    return MinimumCoverageMatrix.model_validate(raw)


def _load_projection(change_dir: Path) -> TraceProjection | None:
    path = change_dir / TRACE_PROJECTION_REL
    if not path.is_file():
        return None
    return TraceProjection.model_validate_json(path.read_text(encoding="utf-8"))


def load_advisory_mrc(change_dir: Path) -> Mapping[str, Any] | None:
    """Load advisory ``minimum_required_coverage`` from explore/ or risk-advisory/."""
    for rel in _ADVISORY_CANDIDATES:
        path = change_dir / rel
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        mrc = raw.get("minimum_required_coverage")
        if isinstance(mrc, Mapping):
            return mrc
    return None


def _load_data_knowledge(project_root: Path) -> DataKnowledge | Mapping[str, Any] | None:
    path = project_root / DATA_KNOWLEDGE_REL
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if raw is None:
        return None
    try:
        return DataKnowledge.model_validate(raw)
    except ValueError:
        return raw if isinstance(raw, Mapping) else None


def _journey_keys_from_advisory(mrc: Mapping[str, Any] | None) -> frozenset[str]:
    if not mrc:
        return frozenset()
    keys: list[str] = []
    for category in ("e2e_if_enabled", "e2e"):
        entries = mrc.get(category) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                keys.append(entry)
            elif isinstance(entry, Mapping) and isinstance(entry.get("key"), str):
                keys.append(entry["key"])
    return journey_known_keys(keys)


def materialize_minimum_coverage_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Publish ``report/minimum-coverage-result.json`` from matrix × projection.

    Fail-open (succeed + ``written: false``) when the matrix or the whole
    projection is absent — same shape as ``no_mrc_matrix``. Corrupt readable
    inputs still fail closed via ``invalid_input``.
    """
    del task  # contract target only
    change_dir = workspace.change_dir
    try:
        matrix = _load_matrix(change_dir)
    except (OSError, ValueError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot read MRC matrix: {err}")
    if matrix is None or not matrix.root:
        return TaskResult(
            status="succeeded",
            value={"change_id": context.change_id, "written": False, "reason": "no_mrc_matrix"},
        )

    try:
        projection = _load_projection(change_dir)
    except (OSError, ValueError) as err:
        return task_failure("invalid_input", f"cannot read trace projection: {err}")
    if projection is None:
        return TaskResult(
            status="succeeded",
            value={
                "change_id": context.change_id,
                "written": False,
                "reason": "no_trace_projection",
            },
        )

    advisory_mrc = load_advisory_mrc(change_dir)
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(advisory_mrc)
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    if lifted.findings:
        # Category cannot be resolved for at least one row — fail closed rather
        # than invent ``api`` and skip closed-key checks.
        keys = ", ".join(finding.key for finding in lifted.findings)
        return task_failure(
            "invalid_input",
            f"mrc_category_unresolved: {keys}",
        )

    knowledge = _load_data_knowledge(workspace.project_root)
    constraint_keys = constraint_known_keys(knowledge) if knowledge is not None else frozenset()
    auth_keys = auth_known_keys(knowledge) if knowledge is not None else frozenset()
    journey_keys = _journey_keys_from_advisory(advisory_mrc)
    closed_findings = mrc_closed_key_findings(
        lifted.obligations,
        constraint_keys=constraint_keys,
        auth_keys=auth_keys,
        journey_keys=journey_keys,
    )

    result = materialize_minimum_coverage(
        change_id=context.change_id,
        obligations=lifted.obligations,
        projection=projection,
        findings=closed_findings,
    )
    out = change_dir / MINIMUM_COVERAGE_RESULT_REL
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(out, canonical_json_bytes(result))
    except OSError as err:
        return task_failure("internal", f"cannot publish minimum-coverage-result: {err}")

    return TaskResult(
        status="succeeded",
        value={
            "change_id": result.change_id,
            "written": True,
            "path": MINIMUM_COVERAGE_RESULT_REL,
            "summary": json.loads(result.summary.model_dump_json()),
            "findings": [f.model_dump(mode="json") for f in result.findings],
        },
    )


__all__ = [
    "MINIMUM_COVERAGE_MATRIX_REL",
    "MINIMUM_COVERAGE_RESULT_REL",
    "TRACE_PROJECTION_REL",
    "CaseExecutionJoin",
    "LiftedObligations",
    "collect_case_executions",
    "execution_counts_for_current_batch",
    "load_advisory_mrc",
    "materialize_minimum_coverage",
    "materialize_minimum_coverage_operation",
    "obligations_from_matrix",
    "projection_rows_by_case_id",
    "shadow_compare_minimum_coverage",
]
