"""``collect-pr-metrics-batch`` / ``materialize-pr-metrics`` (Task 7).

After each ``run-tests`` (initial + healing rerun) the collect bundle writes only
batch-scoped evidence under ``execution/runs/<batch>/`` by invoking the five
Task-6 collectors. After healing completes, ``materialize-pr-metrics`` selects
the latest *complete* batch and is the **only** writer of
``inspect/metrics.json`` — a second call never overwrites that path (healing
must not cause a second authoritative write).

Graph placement (Task 8): ``execution``/``rerun`` → collect → … → healing →
materialize → ``metrics-sufficiency-gate`` → report. This module owns the
callables + contracts; the gate calls ``evaluate_metrics_sufficiency``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.cases import CaseEntry
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricKey,
    MetricsDocument,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    PerfSlackEvidence,
)
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.artifacts.policy import policy_digest as compute_policy_digest
from assurance_agent.evidence.metrics import (
    AUTH_EVIDENCE,
    CONSTRAINT_EVIDENCE,
    DEFAULT_NIGHTLY_KEYS,
    DEFAULT_PR_KEYS,
    DIFF_EVIDENCE,
    JOURNEY_EVIDENCE,
    SLACK_EVIDENCE,
    aggregate_pr_metrics,
)
from assurance_agent.evidence.risk_tier import resolve_risk_tier
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.auth_matrix import compute_auth_matrix_operation
from assurance_agent.workflow.metrics.batch_io import batch_runs_dir, resolve_batch_id, write_batch_evidence
from assurance_agent.workflow.metrics.case_inputs import CaseArtifactError, load_case_entries_strict
from assurance_agent.workflow.metrics.constraint_coverage import compute_constraint_coverage_operation
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage_operation
from assurance_agent.workflow.metrics.journey_coverage import compute_journey_coverage_operation
from assurance_agent.workflow.metrics.threshold_slack import compute_threshold_slack_operation

PR_EVIDENCE_FILES: tuple[str, ...] = (
    DIFF_EVIDENCE,
    CONSTRAINT_EVIDENCE,
    AUTH_EVIDENCE,
    JOURNEY_EVIDENCE,
    SLACK_EVIDENCE,
)

INSPECT_METRICS_REL = "inspect/metrics.json"
METRICS_SOURCE_BATCH_REL = "inspect/metrics-source-batch.json"
BATCH_METRICS_REL = "metrics.json"

_COLLECTORS = (
    collect_diff_coverage_operation,
    compute_constraint_coverage_operation,
    compute_auth_matrix_operation,
    compute_journey_coverage_operation,
    compute_threshold_slack_operation,
)

MaterializeReason = Literal[
    "written",
    "already_materialized",
    "source_batch_recovered",
    "stale_source_batch",
    "older_batch_refused",
    "no_complete_batch",
]


@dataclass(frozen=True)
class MaterializeResult:
    written: bool
    batch_id: str | None
    reason: MaterializeReason
    document: MetricsDocument | None = None


def is_complete_batch(batch_dir: Path) -> bool:
    """True when all five PR evidence files exist under ``batch_dir``."""
    return all((batch_dir / name).is_file() for name in PR_EVIDENCE_FILES)


def select_latest_complete_batch(change_dir: Path) -> str | None:
    """Return the lexicographically greatest complete batch id, or None."""
    complete = _complete_batch_ids(change_dir)
    return complete[-1] if complete else None


def _complete_batch_ids(change_dir: Path) -> list[str]:
    """Return every complete batch id in deterministic order."""
    runs = change_dir / "execution" / "runs"
    if not runs.is_dir():
        return []
    return sorted(path.name for path in runs.iterdir() if path.is_dir() and is_complete_batch(path))


def _matching_metrics_source_batch(change_dir: Path, authoritative: bytes) -> str | None:
    """Find the newest complete batch whose derived metrics exactly match."""
    matches: list[str] = []
    for candidate in _complete_batch_ids(change_dir):
        batch_copy = batch_runs_dir(change_dir, candidate) / BATCH_METRICS_REL
        try:
            if batch_copy.is_file() and batch_copy.read_bytes() == authoritative:
                matches.append(candidate)
        except OSError:
            continue
    return matches[-1] if matches else None


def _corrupt_gap(*, metric: MetricKey, path: Path, err: Exception) -> MetricCollectionGap:
    return MetricCollectionGap(
        code="artifact_corrupt",
        metric=metric,
        detail=f"{path.name} unparseable: {err}",
    )


def _load_batch_evidence_file(
    path: Path,
    model: type[Any],
    *,
    change_id: str,
    batch_id: str,
    metric: MetricKey,
    corrupt_factory: Any,
) -> Any | None:
    """Load one evidence file; present-but-corrupt → typed ``artifact_corrupt`` stub.

    Absent files stay ``None`` (aggregate maps that to ``… missing``). A present
    but unparseable file must never collapse to that missing path.
    """
    if not path.is_file():
        return None
    try:
        return model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as err:
        return corrupt_factory(
            change_id=change_id,
            batch_id=batch_id,
            gap=_corrupt_gap(metric=metric, path=path, err=err),
        )


def _corrupt_coverage_diff(
    *, change_id: str, batch_id: str, gap: MetricCollectionGap
) -> CoverageDiffEvidence:
    return CoverageDiffEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        total_changed_lines=0,
        covered_changed_lines=0,
        value=None,
        collection_gaps=(gap,),
    )


def _corrupt_constraint(
    *, change_id: str, batch_id: str, gap: MetricCollectionGap
) -> ConstraintCoverageEvidence:
    return ConstraintCoverageEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=None,
        collection_gaps=(gap,),
    )


def _corrupt_auth(*, change_id: str, batch_id: str, gap: MetricCollectionGap) -> AuthMatrixEvidence:
    return AuthMatrixEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=None,
        collection_gaps=(gap,),
    )


def _corrupt_journey(*, change_id: str, batch_id: str, gap: MetricCollectionGap) -> JourneyCoverageEvidence:
    return JourneyCoverageEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=None,
        collection_gaps=(gap,),
    )


def _corrupt_slack(*, change_id: str, batch_id: str, gap: MetricCollectionGap) -> PerfSlackEvidence:
    return PerfSlackEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        value=None,
        collection_gaps=(gap,),
    )


def load_batch_evidence(
    change_dir: Path,
    batch_id: str,
    *,
    change_id: str,
) -> tuple[
    CoverageDiffEvidence | None,
    ConstraintCoverageEvidence | None,
    AuthMatrixEvidence | None,
    JourneyCoverageEvidence | None,
    PerfSlackEvidence | None,
]:
    batch = batch_runs_dir(change_dir, batch_id)
    return (
        _load_batch_evidence_file(
            batch / DIFF_EVIDENCE,
            CoverageDiffEvidence,
            change_id=change_id,
            batch_id=batch_id,
            metric="diff_coverage",
            corrupt_factory=_corrupt_coverage_diff,
        ),
        _load_batch_evidence_file(
            batch / CONSTRAINT_EVIDENCE,
            ConstraintCoverageEvidence,
            change_id=change_id,
            batch_id=batch_id,
            metric="constraint_coverage",
            corrupt_factory=_corrupt_constraint,
        ),
        _load_batch_evidence_file(
            batch / AUTH_EVIDENCE,
            AuthMatrixEvidence,
            change_id=change_id,
            batch_id=batch_id,
            metric="auth_matrix_coverage",
            corrupt_factory=_corrupt_auth,
        ),
        _load_batch_evidence_file(
            batch / JOURNEY_EVIDENCE,
            JourneyCoverageEvidence,
            change_id=change_id,
            batch_id=batch_id,
            metric="journey_coverage",
            corrupt_factory=_corrupt_journey,
        ),
        _load_batch_evidence_file(
            batch / SLACK_EVIDENCE,
            PerfSlackEvidence,
            change_id=change_id,
            batch_id=batch_id,
            metric="threshold_slack",
            corrupt_factory=_corrupt_slack,
        ),
    )


def _read_source_batch(change_dir: Path) -> str | None:
    path = change_dir / METRICS_SOURCE_BATCH_REL
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    batch_id = payload.get("batch_id")
    return batch_id if isinstance(batch_id, str) and batch_id else None


def build_metrics_document(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    computed_at: datetime,
    batch_id: str,
    policy_digest: str | None = None,
    cases: list[CaseEntry] | None = None,
) -> MetricsDocument:
    """Aggregate PR metrics for ``batch_id`` into an in-memory ``MetricsDocument``.

    Performs digest / risk / evidence load / aggregate only — writes nothing.
    """
    digest = policy_digest if policy_digest is not None else _policy_digest(project_root)

    risk = resolve_risk_tier(cases if cases is not None else load_case_entries_strict(change_dir))
    coverage_diff, constraint, auth, journey, slack = load_batch_evidence(
        change_dir, batch_id, change_id=change_id
    )
    return aggregate_pr_metrics(
        change_id=change_id,
        computed_at=computed_at,
        policy_digest=digest,
        risk=risk,
        coverage_diff=coverage_diff,
        constraint_coverage=constraint,
        auth_matrix=auth,
        journey_coverage=journey,
        perf_slack=slack,
        pr_keys=DEFAULT_PR_KEYS,
        nightly_keys=DEFAULT_NIGHTLY_KEYS,
        expected_batch_id=batch_id,
    )


def materialize_pr_metrics(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    computed_at: datetime,
    policy_digest: str | None = None,
    batch_id: str | None = None,
    cases: list[CaseEntry] | None = None,
) -> MaterializeResult:
    """Aggregate the selected complete batch and uniquely write ``inspect/metrics.json``.

    Write-once: if the authoritative path already exists, refuse to replace it.
    Always-latest: an explicit ``batch_id`` older than the newest complete batch is
    refused (``older_batch_refused``), whether or not ``inspect/metrics.json`` exists.
    A document that already exists but was aggregated from something other than the
    newest complete batch is reported as ``stale_source_batch`` rather than a plain
    ``already_materialized``: immutability means it cannot be refreshed here, so the
    caller must refuse rather than adjudicate metrics the evidence has moved past.
    Collectors never touch this path.
    """
    latest = select_latest_complete_batch(change_dir)
    selected = batch_id or latest
    if selected is None:
        return MaterializeResult(written=False, batch_id=None, reason="no_complete_batch")

    if not is_complete_batch(batch_runs_dir(change_dir, selected)):
        return MaterializeResult(written=False, batch_id=selected, reason="no_complete_batch")

    if batch_id is not None and latest is not None and batch_id < latest:
        return MaterializeResult(written=False, batch_id=selected, reason="older_batch_refused")

    existing_path = change_dir / INSPECT_METRICS_REL
    source_batch = _read_source_batch(change_dir)
    if existing_path.is_file():
        if source_batch is not None and latest is not None and source_batch != latest:
            return MaterializeResult(written=False, batch_id=source_batch, reason="stale_source_batch")
        if source_batch is None:
            try:
                authoritative = existing_path.read_bytes()
            except OSError:
                authoritative = b""
            recovered_batch = _matching_metrics_source_batch(change_dir, authoritative)
            if recovered_batch is None:
                return MaterializeResult(
                    written=False,
                    batch_id=selected,
                    reason="stale_source_batch",
                )
            atomic_write_bytes(
                change_dir / METRICS_SOURCE_BATCH_REL,
                canonical_json_bytes({"batch_id": recovered_batch, "change_id": change_id}),
            )
            if latest is not None and recovered_batch != latest:
                return MaterializeResult(
                    written=False,
                    batch_id=recovered_batch,
                    reason="stale_source_batch",
                )
            return MaterializeResult(
                written=False,
                batch_id=recovered_batch,
                reason="source_batch_recovered",
                document=MetricsDocument.model_validate_json(authoritative),
            )
        return MaterializeResult(
            written=False, batch_id=source_batch or selected, reason="already_materialized"
        )

    document = build_metrics_document(
        change_dir=change_dir,
        project_root=project_root,
        change_id=change_id,
        computed_at=computed_at,
        batch_id=selected,
        policy_digest=policy_digest,
        cases=cases,
    )

    # Derived pre-copy under the batch (unregistered); then the authoritative path.
    write_batch_evidence(change_dir, selected, BATCH_METRICS_REL, document)
    out = change_dir / INSPECT_METRICS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(document))
    atomic_write_bytes(
        change_dir / METRICS_SOURCE_BATCH_REL,
        canonical_json_bytes({"batch_id": selected, "change_id": change_id}),
    )
    return MaterializeResult(written=True, batch_id=selected, reason="written", document=document)


def _policy_digest(project_root: Path) -> str:
    return compute_policy_digest(load_policy(project_root))


def collect_pr_metrics_batch_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Run the five PR collectors for the current (or explicit) batch.

    Writes only under ``execution/runs/<batch>/`` — never ``inspect/metrics.json``.
    """
    try:
        batch_id = resolve_batch_id(
            workspace.change_dir,
            explicit=str(task_with(task).get("batch_id") or "") or None,
        )
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot resolve batch_id: {err}")

    child = task.model_copy(
        update={
            "input": {"with": {**task_with(task), "batch_id": batch_id}},
        }
    )
    results: list[dict[str, Any]] = []
    for collector in _COLLECTORS:
        result = collector(child, workspace, context)
        results.append({"target": collector.__name__, "status": result.status})
        if result.status != "succeeded":
            return task_failure(
                "invalid_input",
                f"collect-pr-metrics-batch stopped at {collector.__name__}: {result.error}",
            )

    return TaskResult(
        status="succeeded",
        value={
            "batch_id": batch_id,
            "collectors": results,
            "evidence_dir": f"execution/runs/{batch_id}",
        },
    )


def materialize_pr_metrics_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Select the latest complete batch and uniquely write ``inspect/metrics.json``."""
    change_id = context.change_id or workspace.change_dir.name
    explicit = str(task_with(task).get("batch_id") or "") or None
    try:
        digest = _policy_digest(workspace.project_root)
    except Exception as err:  # noqa: BLE001 — surface as invalid_input
        return task_failure("invalid_input", f"cannot load policy digest: {err}")

    try:
        result = materialize_pr_metrics(
            change_dir=workspace.change_dir,
            project_root=workspace.project_root,
            change_id=change_id,
            computed_at=datetime.now(tz=UTC),
            policy_digest=digest,
            batch_id=explicit,
        )
    except CaseArtifactError as err:
        return task_failure("invalid_input", str(err))
    if result.reason == "no_complete_batch":
        return task_failure(
            "invalid_input",
            "no complete PR metrics batch under execution/runs/",
        )
    if result.reason == "stale_source_batch":
        latest = select_latest_complete_batch(workspace.change_dir)
        return task_failure(
            "invalid_input",
            f"{INSPECT_METRICS_REL} was aggregated from batch {result.batch_id!r} but "
            f"batch {latest!r} is now the newest complete one; the authoritative "
            "document is write-once and cannot be refreshed in place",
        )
    return TaskResult(
        status="succeeded",
        value={
            "written": result.written,
            "batch_id": result.batch_id,
            "reason": result.reason,
            "path": INSPECT_METRICS_REL,
        },
    )


__all__ = [
    "BATCH_METRICS_REL",
    "INSPECT_METRICS_REL",
    "METRICS_SOURCE_BATCH_REL",
    "PR_EVIDENCE_FILES",
    "MaterializeResult",
    "build_metrics_document",
    "collect_pr_metrics_batch_operation",
    "is_complete_batch",
    "load_batch_evidence",
    "materialize_pr_metrics",
    "materialize_pr_metrics_operation",
    "select_latest_complete_batch",
]
