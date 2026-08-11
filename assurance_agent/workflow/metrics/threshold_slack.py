"""``operation:compute-threshold-slack`` — threshold/measured slack (§5-B5).

Slack above the default band (10) produces a shortboard only — never a hard fail.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import yaml

from assurance_agent.artifacts.models.inspect import PerformanceScenarioVerdict
from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import (
    PerfSlackEvidence,
    PerfSlackScenario,
)
from assurance_agent.workflow.execution.results import PerformanceResult
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import (
    batch_runs_dir,
    resolve_batch_id,
    write_batch_evidence,
)

PERF_SLACK_REL = "perf-slack.json"
DEFAULT_SLACK_BAND = 10.0


def compute_threshold_slack(
    *,
    change_id: str,
    batch_id: str,
    scenarios: Sequence[PerformanceScenarioVerdict],
    slack_band: float = DEFAULT_SLACK_BAND,
) -> PerfSlackEvidence:
    """Compute threshold/measured per scenario; max slack is the published value."""
    if not scenarios:
        return PerfSlackEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            value=None,
            slack_band=slack_band,
            scenarios=(),
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="threshold_slack",
                    detail="no performance scenarios available",
                ),
            ),
        )

    malformed = [
        field
        for index, scenario in enumerate(scenarios)
        for field, value in (
            (f"scenarios[{index}].capability", scenario.capability),
            (f"scenarios[{index}].endpoint", scenario.endpoint),
        )
        if not value.strip()
    ]
    if malformed:
        return PerfSlackEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            value=None,
            slack_band=slack_band,
            scenarios=(),
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="threshold_slack",
                    detail="performance scenarios have empty identity fields: " + ", ".join(malformed),
                ),
            ),
        )

    rows: list[PerfSlackScenario] = []
    slacks: list[float] = []
    for scenario in scenarios:
        measured = scenario.measured_p95_ms
        slack: float | None
        if measured is None or measured <= 0:
            slack = None
        else:
            slack = scenario.threshold_p95_ms / measured
            slacks.append(slack)
        rows.append(
            PerfSlackScenario(
                capability=scenario.capability,
                endpoint=scenario.endpoint,
                threshold_p95_ms=scenario.threshold_p95_ms,
                measured_p95_ms=measured,
                slack=slack,
            )
        )

    if not slacks:
        return PerfSlackEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            value=None,
            slack_band=slack_band,
            scenarios=tuple(rows),
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="threshold_slack",
                    detail="no measurable p95 values to compute slack",
                ),
            ),
        )

    value = max(slacks)
    shortboards: tuple[MetricShortboard, ...] = ()
    if value > slack_band:
        shortboards = (
            MetricShortboard(
                code="threshold_slack_out_of_band",
                metric="threshold_slack",
                detail=f"max slack {value:.4g} exceeds band {slack_band:g}",
            ),
        )

    return PerfSlackEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        value=value,
        slack_band=slack_band,
        scenarios=tuple(rows),
        collection_gaps=(),
        shortboards=shortboards,
    )


def compute_threshold_slack_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    change_dir = workspace.change_dir
    try:
        batch_id = resolve_batch_id(
            change_dir,
            explicit=str(task_with(task).get("batch_id") or "") or None,
        )
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot resolve batch_id: {err}")

    band_raw = task_with(task).get("slack_band")
    slack_band = (
        float(band_raw) if isinstance(band_raw, (int, float, str)) and band_raw != "" else DEFAULT_SLACK_BAND
    )

    perf_path = batch_runs_dir(change_dir, batch_id) / "performance-result.json"
    if not perf_path.is_file():
        evidence = PerfSlackEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            value=None,
            slack_band=slack_band,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="threshold_slack",
                    detail="performance-result.json missing for batch",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, PERF_SLACK_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    try:
        result = PerformanceResult.model_validate_json(perf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as err:
        evidence = PerfSlackEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            value=None,
            slack_band=slack_band,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="threshold_slack",
                    detail=f"cannot parse performance-result.json: {err}",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, PERF_SLACK_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    evidence = compute_threshold_slack(
        change_id=context.change_id,
        batch_id=batch_id,
        scenarios=result.scenarios,
        slack_band=slack_band,
    )
    write_batch_evidence(change_dir, batch_id, PERF_SLACK_REL, evidence)
    return _ok(context.change_id, batch_id, evidence)


def _ok(change_id: str, batch_id: str, evidence: PerfSlackEvidence) -> TaskResult:
    return TaskResult(
        status="succeeded",
        value={
            "change_id": change_id,
            "batch_id": batch_id,
            "written": True,
            "path": f"execution/runs/{batch_id}/{PERF_SLACK_REL}",
            "value": evidence.value,
            "collection_gaps": len(evidence.collection_gaps),
            "shortboards": len(evidence.shortboards),
        },
    )


__all__ = [
    "DEFAULT_SLACK_BAND",
    "PERF_SLACK_REL",
    "compute_threshold_slack",
    "compute_threshold_slack_operation",
]
