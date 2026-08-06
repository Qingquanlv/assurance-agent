"""``operation:compute-journey-coverage`` — MRC journey × case × execution (§5-A4).

Quarantined journeys never count as covered but remain in the declared
denominator. Active keys are loaded from ``inspect/quarantine-projection.json``
(plus optional ``with.quarantine``). Covered requires Task 3 B2 strong e2e
oracle; missing oracle sources fail closed (never default to strong).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricScope,
    MetricShortboard,
)
from assurance_agent.artifacts.models.minimum_coverage import (
    MinimumCoverageMatrix,
    MrcObligation,
    journey_known_keys,
    maps_from_advisory_mrc,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    JourneyCoverageEvidence,
    JourneyCoverageItem,
)
from assurance_agent.artifacts.models.trace import TraceProjection, TraceRow
from assurance_agent.verification.assertion_class import (
    classify_e2e_assertions,
    counts_as_covered_oracle,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import (
    batch_runs_dir,
    resolve_batch_id,
    write_batch_evidence,
)
from assurance_agent.workflow.metrics.minimum_coverage import (
    MINIMUM_COVERAGE_MATRIX_REL,
    TRACE_PROJECTION_REL,
    collect_case_executions,
    load_advisory_mrc,
    obligations_from_matrix,
    projection_rows_by_case_id,
)
from assurance_agent.workflow.metrics.quarantine import (
    QuarantineIntegrityError,
    load_active_quarantine_keys,
)

JOURNEY_COVERAGE_REL = "journey-coverage.json"
_JOURNEY_CATEGORIES = frozenset({"e2e", "e2e_if_enabled"})
# Written into the batch directory by `aa run` before it builds the gate
# (`workflow.execution.runner.TRACE_PROJECTION_NAME`). Spelled out rather than
# imported: this module is downstream of the runner, and importing it back would
# close a package cycle for one filename.
_BATCH_TRACE_PROJECTION_NAME = "trace-projection.json"
OracleStrength = Literal["strong", "weak", "unavailable"]


def compute_journey_coverage(
    *,
    change_id: str,
    batch_id: str,
    obligations: Sequence[MrcObligation],
    projection: TraceProjection,
    quarantine: Sequence[str] = (),
    test_sources: Mapping[str, str] | None = None,
    case_functions: Mapping[str, tuple[str, str]] | None = None,
) -> JourneyCoverageEvidence:
    """Join journey MRC obligations with current-batch execution; quarantine may be empty."""
    quarantined = frozenset(quarantine)
    journey_obs = [item for item in obligations if item.category in _JOURNEY_CATEGORIES]
    closed = journey_known_keys(item.key for item in journey_obs)

    if not journey_obs:
        return JourneyCoverageEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="journey_coverage",
                    detail="no journey MRC obligations declared",
                ),
            ),
        )

    rows_by_id = projection_rows_by_case_id(projection)
    items: list[JourneyCoverageItem] = []
    covered_keys: set[str] = set()

    for obligation in journey_obs:
        if obligation.key in quarantined:
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=(),
                    covered=False,
                    quarantined=True,
                    status="quarantined",
                )
            )
            continue
        if obligation.skipped_by_scope:
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=(),
                    covered=False,
                    status="skipped_by_scope",
                )
            )
            continue
        if not obligation.case_ids:
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=(),
                    executed_case_ids=(),
                    covered=False,
                    status="missing",
                )
            )
            continue

        joined = collect_case_executions(
            obligation.case_ids,
            rows_by_id,
            batch_id=batch_id,
        )
        executed = joined.executed_case_ids
        statuses = joined.statuses

        if not executed:
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=(),
                    covered=False,
                    status="not_executed",
                )
            )
            continue
        if any(status == "failed" for status in statuses):
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=executed,
                    covered=False,
                    status="covered_but_failing",
                )
            )
            continue

        strength = _journey_oracle_strength(
            executed,
            test_sources=test_sources,
            case_functions=case_functions,
            rows_by_id=rows_by_id,
        )
        if strength == "unavailable":
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=executed,
                    covered=False,
                    status="oracle_unavailable",
                )
            )
            continue
        if strength == "weak":
            items.append(
                JourneyCoverageItem(
                    journey_key=obligation.key,
                    case_ids=obligation.case_ids,
                    executed_case_ids=executed,
                    covered=False,
                    status="weak_oracle",
                )
            )
            continue

        covered_keys.add(obligation.key)
        items.append(
            JourneyCoverageItem(
                journey_key=obligation.key,
                case_ids=obligation.case_ids,
                executed_case_ids=executed,
                covered=True,
                status="covered",
            )
        )

    declared_keys = tuple(sorted(closed))
    declared = MetricScope.of(
        total=len(declared_keys),
        covered=len(covered_keys),
        uncovered=tuple(sorted(set(declared_keys) - covered_keys)),
    )
    shortboards = tuple(
        MetricShortboard(
            code="quarantined_excluded",
            metric="journey_coverage",
            detail=key,
        )
        for key in sorted(quarantined & closed)
    )
    return JourneyCoverageEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=declared,
        touched=declared,
        value=declared.value,
        items=tuple(items),
        collection_gaps=(),
        shortboards=shortboards,
    )


def _journey_oracle_strength(
    executed_case_ids: Sequence[str],
    *,
    test_sources: Mapping[str, str] | None,
    case_functions: Mapping[str, tuple[str, str]] | None,
    rows_by_id: Mapping[str, TraceRow],
) -> OracleStrength:
    """Classify B2 e2e oracle strength for executed cases.

    Fail closed: omitted / empty sources are ``unavailable``, never strong.
    """
    if not test_sources or not case_functions:
        return "unavailable"

    saw_classifiable = False
    for case_id in executed_case_ids:
        ref = case_functions.get(case_id)
        if ref is None:
            row = rows_by_id.get(case_id)
            if row is None or not row.covering_tests:
                continue
            file = row.covering_tests[0].file
            function_name = row.covering_tests[0].test_name
        else:
            file, function_name = ref
        source = test_sources.get(file)
        if source is None:
            continue
        saw_classifiable = True
        if counts_as_covered_oracle(classify_e2e_assertions(source, function_name=function_name)):
            return "strong"
    if not saw_classifiable:
        return "unavailable"
    return "weak"


def _resolve_projection_path(change_dir: Path, batch_id: str) -> Path | None:
    """The batch's own trace projection, else the reconciled change-wide one.

    ``inspect/trace-projection.json`` is published by
    ``operation:materialize-trace-projection``, which the assurance graph runs
    *after* this collector — so on a change's first batch it does not exist yet and
    reading only that path made A4 structurally uncollectable. The copy ``aa run``
    folds into the batch directory is both already on disk and same-batch by
    construction, so it wins; the reconciled document remains the fallback for
    callers that hold one (a healing rerun, or a direct invocation).
    """
    batch_scoped = batch_runs_dir(change_dir, batch_id) / _BATCH_TRACE_PROJECTION_NAME
    if batch_scoped.is_file():
        return batch_scoped
    reconciled = change_dir / TRACE_PROJECTION_REL
    return reconciled if reconciled.is_file() else None


def compute_journey_coverage_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    change_dir = workspace.change_dir
    project_root = workspace.project_root
    try:
        batch_id = resolve_batch_id(
            change_dir,
            explicit=str(task_with(task).get("batch_id") or "") or None,
        )
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot resolve batch_id: {err}")

    quarantine_raw = task_with(task).get("quarantine")
    quarantine_from_task: tuple[str, ...]
    if quarantine_raw is None:
        quarantine_from_task = ()
    elif isinstance(quarantine_raw, (list, tuple)):
        quarantine_from_task = tuple(str(item) for item in quarantine_raw)
    else:
        return task_failure("invalid_input", "with.quarantine must be a list when provided")
    try:
        active_quarantine = load_active_quarantine_keys(change_dir)
    except QuarantineIntegrityError as err:
        evidence = JourneyCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="journey_coverage",
                    detail=str(err),
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, JOURNEY_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)
    quarantine = tuple(sorted(set(quarantine_from_task) | set(active_quarantine)))

    matrix_path = change_dir / MINIMUM_COVERAGE_MATRIX_REL
    projection_path = _resolve_projection_path(change_dir, batch_id)
    if not matrix_path.is_file() or projection_path is None:
        evidence = JourneyCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="journey_coverage",
                    detail="missing MRC matrix or trace projection",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, JOURNEY_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    try:
        raw_matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
        matrix = MinimumCoverageMatrix.model_validate([] if raw_matrix is None else raw_matrix)
        projection = TraceProjection.model_validate_json(projection_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as err:
        evidence = JourneyCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="journey_coverage",
                    detail=f"cannot load journey inputs: {err}",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, JOURNEY_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    advisory_mrc = load_advisory_mrc(change_dir)
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(advisory_mrc)
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    if lifted.findings:
        evidence = JourneyCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="journey_coverage",
                    detail=(
                        "mrc_category_unresolved: " + ", ".join(finding.key for finding in lifted.findings)
                    ),
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, JOURNEY_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    case_functions = _case_functions_from_projection(projection)
    test_sources = _load_e2e_sources(project_root, case_functions)
    evidence = compute_journey_coverage(
        change_id=context.change_id,
        batch_id=batch_id,
        obligations=lifted.obligations,
        projection=projection,
        quarantine=quarantine,
        test_sources=test_sources,
        case_functions=case_functions,
    )
    write_batch_evidence(change_dir, batch_id, JOURNEY_COVERAGE_REL, evidence)
    return _ok(context.change_id, batch_id, evidence)


def _case_functions_from_projection(
    projection: TraceProjection,
) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for row in projection.rows:
        if not row.covering_tests:
            continue
        ref = row.covering_tests[0]
        out[row.case_id] = (ref.file, ref.test_name)
    return out


def _load_e2e_sources(
    project_root: Path,
    case_functions: Mapping[str, tuple[str, str]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for file, _name in case_functions.values():
        if file in out:
            continue
        path = Path(file)
        if not path.is_absolute():
            path = project_root / path
        try:
            out[file] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _ok(change_id: str, batch_id: str, evidence: JourneyCoverageEvidence) -> TaskResult:
    return TaskResult(
        status="succeeded",
        value={
            "change_id": change_id,
            "batch_id": batch_id,
            "written": True,
            "path": f"execution/runs/{batch_id}/{JOURNEY_COVERAGE_REL}",
            "value": evidence.value,
            "collection_gaps": len(evidence.collection_gaps),
        },
    )


__all__ = [
    "JOURNEY_COVERAGE_REL",
    "compute_journey_coverage",
    "compute_journey_coverage_operation",
]
