"""``operation:compute-baseline-drift`` — nightly A5 performance baseline drift (§5-A5).

Compares the change's latest verified performance scenarios against project-level
append-only baseline history under ``.aa/baseline/performance/``. Writes batch
evidence at ``execution/runs/nightly/baseline-drift.json``; never writes
``inspect/metrics.json``. Absolute-threshold inconclusive remains a deterministic
stop elsewhere — this collector only emits shortboards / not_evaluated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.inspect import PerformanceScenarioVerdict
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricCollectionGapCode,
    MetricShortboard,
    PR_METRICS_REL,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    BaselineDriftEvidence,
    BaselineDriftScenario,
)
from assurance_agent.verification.baseline_history import (
    DEFAULT_BASELINE_WINDOW,
    DEFAULT_MIN_BASELINE_SAMPLES,
    PerformanceBaselineObservation,
    append_baseline_observation,
    recent_baseline_observations,
    scenario_identity,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.execution.results import PerformanceResult
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import batch_runs_dir, resolve_batch_id

BASELINE_DRIFT_BATCH_ID = "nightly"
BASELINE_DRIFT_EVIDENCE_REL = f"execution/runs/{BASELINE_DRIFT_BATCH_ID}/baseline-drift.json"
DEFAULT_DRIFT_BAND = 0.2


def relative_regression(current: float, baseline: float) -> float:
    """Non-negative relative regression; improvements collapse to 0.0."""
    if baseline <= 0:
        return 0.0 if current <= 0 else 1.0
    delta = (current - baseline) / baseline
    return delta if delta > 0 else 0.0


def compute_baseline_drift(
    *,
    project_root: Path,
    change_id: str,
    change_dir: Path,
    batch_id: str = BASELINE_DRIFT_BATCH_ID,
    drift_band: float = DEFAULT_DRIFT_BAND,
    window: int = DEFAULT_BASELINE_WINDOW,
    min_samples: int = DEFAULT_MIN_BASELINE_SAMPLES,
    source_batch_id: str | None = None,
) -> BaselineDriftEvidence:
    """Compute drift vs recent N; append current verified observations afterward."""
    try:
        resolved_source = source_batch_id or resolve_batch_id(change_dir)
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return _collection_failed(
            change_id=change_id,
            batch_id=batch_id,
            drift_band=drift_band,
            window=window,
            code="collection_failed",
            detail=f"cannot resolve performance batch: {err}",
        )

    perf_path = batch_runs_dir(change_dir, resolved_source) / "performance-result.json"
    source = {
        "performance_result_rel": f"execution/runs/{resolved_source}/performance-result.json",
        "source_batch_id": resolved_source,
        "pr_metrics_rel": PR_METRICS_REL,
        "baseline_history_rel": ".aa/baseline/performance",
    }

    if not perf_path.is_file():
        return BaselineDriftEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            drift_band=drift_band,
            window=window,
            shortboards=(
                MetricShortboard(
                    code="sample_insufficient",
                    metric="baseline_drift",
                    detail="performance-result.json missing for source batch",
                ),
            ),
            source=source,
        )

    try:
        raw_bytes = perf_path.read_bytes()
        result = PerformanceResult.model_validate_json(raw_bytes)
        source_digest = hashlib.sha256(raw_bytes).hexdigest()
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return _collection_failed(
            change_id=change_id,
            batch_id=batch_id,
            drift_band=drift_band,
            window=window,
            code="artifact_corrupt",
            detail=f"cannot parse performance-result.json: {err}",
            source=source,
        )

    verified = _verified_scenarios(result.scenarios)
    if not verified:
        return BaselineDriftEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            drift_band=drift_band,
            window=window,
            shortboards=(
                MetricShortboard(
                    code="sample_insufficient",
                    metric="baseline_drift",
                    detail="no verified performance scenarios with measured p95/error_rate",
                ),
            ),
            source=source,
        )

    rows: list[BaselineDriftScenario] = []
    drifts: list[float] = []
    insufficient = False
    for scenario in verified:
        # Fetch window+1 so excluding the current change/batch still leaves up to
        # ``window`` prior samples (stable mean across same-payload reruns).
        history = recent_baseline_observations(
            project_root,
            identity=scenario_identity(scenario.capability, scenario.endpoint),
            limit=window + 1,
        )
        history = tuple(
            h for h in history if not (h.change_id == change_id and h.batch_id == resolved_source)
        )[:window]
        if len(history) < min_samples:
            insufficient = True
            continue
        sample = history
        baseline_p95 = sum(h.p95_ms for h in sample) / len(sample)
        baseline_err = sum(h.error_rate for h in sample) / len(sample)
        assert scenario.measured_p95_ms is not None
        assert scenario.measured_error_rate is not None
        drift = max(
            relative_regression(scenario.measured_p95_ms, baseline_p95),
            relative_regression(scenario.measured_error_rate, baseline_err),
        )
        rows.append(
            BaselineDriftScenario(
                capability=scenario.capability,
                endpoint=scenario.endpoint,
                current_p95_ms=scenario.measured_p95_ms,
                baseline_p95_ms=baseline_p95,
                current_error_rate=scenario.measured_error_rate,
                baseline_error_rate=baseline_err,
                drift=drift,
                sample_count=len(sample),
            )
        )
        drifts.append(drift)

    # Append current verified observations as immutable receipts (never overwrite),
    # including when the metric itself is not_evaluated for insufficient samples.
    for scenario in verified:
        assert scenario.measured_p95_ms is not None
        assert scenario.measured_error_rate is not None
        append_baseline_observation(
            project_root,
            PerformanceBaselineObservation(
                capability=scenario.capability,
                endpoint=scenario.endpoint,
                change_id=change_id,
                batch_id=resolved_source,
                p95_ms=scenario.measured_p95_ms,
                error_rate=scenario.measured_error_rate,
                source_digest=source_digest,
            ),
        )

    # Whole-metric not_evaluated when any verified scenario lacks min_samples —
    # do not publish a warm-subset evaluation while cold scenarios are omitted.
    if insufficient or not rows:
        return BaselineDriftEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            drift_band=drift_band,
            window=window,
            shortboards=(
                MetricShortboard(
                    code="sample_insufficient",
                    metric="baseline_drift",
                    detail=(f"need {min_samples} verified baseline samples per scenario (window={window})"),
                ),
            ),
            source=source,
        )

    value = max(drifts)
    shortboards: tuple[MetricShortboard, ...] = ()
    if value > drift_band:
        shortboards = (
            MetricShortboard(
                code="baseline_drift_out_of_band",
                metric="baseline_drift",
                detail=f"max drift {value:.4g} exceeds band {drift_band:g}",
            ),
        )

    return BaselineDriftEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="evaluated",
        value=value,
        drift_band=drift_band,
        window=window,
        scenarios=tuple(rows),
        shortboards=shortboards,
        source=source,
    )


def compute_baseline_drift_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Side-effecting op: compute drift, append history, write nightly evidence."""
    del task
    change_id = context.change_id or workspace.change_dir.name
    evidence = compute_baseline_drift(
        project_root=workspace.project_root,
        change_id=change_id,
        change_dir=workspace.change_dir,
    )
    out = workspace.change_dir / BASELINE_DRIFT_EVIDENCE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(evidence))
    return TaskResult(
        status="succeeded",
        value={
            "path": BASELINE_DRIFT_EVIDENCE_REL,
            "status": evidence.status,
            "value": evidence.value,
            "shortboards": len(evidence.shortboards),
            "collection_gaps": len(evidence.collection_gaps),
        },
    )


def _verified_scenarios(
    scenarios: Sequence[PerformanceScenarioVerdict],
) -> tuple[PerformanceScenarioVerdict, ...]:
    """Scenarios with measured p95 and error_rate (absolute inconclusive handled elsewhere)."""
    return tuple(s for s in scenarios if s.measured_p95_ms is not None and s.measured_error_rate is not None)


def _collection_failed(
    *,
    change_id: str,
    batch_id: str,
    drift_band: float,
    window: int,
    code: MetricCollectionGapCode,
    detail: str,
    source: dict[str, str] | None = None,
) -> BaselineDriftEvidence:
    return BaselineDriftEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="collection_failed",
        value=None,
        drift_band=drift_band,
        window=window,
        collection_gaps=(
            MetricCollectionGap(
                code=code,
                metric="baseline_drift",
                detail=detail,
            ),
        ),
        source=source or {},
    )


__all__ = [
    "BASELINE_DRIFT_BATCH_ID",
    "BASELINE_DRIFT_EVIDENCE_REL",
    "DEFAULT_DRIFT_BAND",
    "compute_baseline_drift",
    "compute_baseline_drift_operation",
    "relative_regression",
]
