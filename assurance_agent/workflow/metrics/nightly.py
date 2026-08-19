"""Nightly metrics carrier (M2 collectors + M3 adversarial yield + aggregate).

Graph entrypoint ``metrics-nightly`` loads the latest PR metrics document, runs
mutation / assertion / baseline-drift / adversarial-yield collectors, aggregates
into ``inspect/metrics-nightly.json``, and evaluates retrospective shortboards.
Collectors and aggregation must **never** write ``inspect/metrics.json`` — a
passed PR verdict stays write-once (§7 / §9).

Mutation sampling, assertion-strength, baseline-drift, and adversarial-yield
live in sibling modules and are re-exported here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import (
    NIGHTLY_METRICS_REL,
    PR_METRICS_REL,
    MetricCollectionGap,
    MetricKey,
    MetricsDocument,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    BaselineDriftEvidence,
    MutationEvidence,
)
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.artifacts.policy import policy_digest as compute_policy_digest
from assurance_agent.evidence.metrics import aggregate_nightly_metrics
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.evidence.risk_tier import RiskTierResolution, resolve_risk_tier
from assurance_agent.verification.baseline_history import DEFAULT_BASELINE_WINDOW
from assurance_agent.verification.mutation_runner import MutationTool
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.adversarial_yield import (
    ADVERSARIAL_YIELD_BATCH_ID,
    ADVERSARIAL_YIELD_EVIDENCE_REL,
    collect_adversarial_yield_operation,
)
from assurance_agent.workflow.metrics.assertion_strength import (
    ASSERTION_STRENGTH_BATCH_ID,
    ASSERTION_STRENGTH_EVIDENCE_REL,
    compute_assertion_strength_operation,
)
from assurance_agent.workflow.metrics.baseline_drift import (
    BASELINE_DRIFT_BATCH_ID,
    BASELINE_DRIFT_EVIDENCE_REL,
    DEFAULT_DRIFT_BAND,
    compute_baseline_drift_operation,
)
from assurance_agent.workflow.metrics.c_layer import materialize_c_layer_metrics_operation
from assurance_agent.workflow.metrics.case_inputs import CaseArtifactError, load_case_entries_strict
from assurance_agent.workflow.metrics.mutation import (
    MUTATION_BATCH_ID,
    MUTATION_EVIDENCE_REL,
    run_mutation_sample_operation,
)
from assurance_agent.workflow.metrics.quarantine import materialize_quarantine_projection_operation

NIGHTLY_SOURCE_REL = "inspect/metrics-nightly-source.json"
NIGHTLY_SHORTBOARDS_REL = "inspect/metrics-nightly-shortboards.json"

# Schema order for ``metrics-nightly-workflow`` (pinned by entrypoint tests).
NIGHTLY_GRAPH_TARGETS: tuple[str, ...] = (
    "operation:load-latest-pr-metrics",
    "operation:run-mutation-sample",
    "operation:compute-assertion-strength",
    "operation:compute-baseline-drift",
    "operation:collect-adversarial-yield",
    "operation:materialize-quarantine-projection",
    "operation:materialize-c-layer-metrics",
    "operation:aggregate-nightly-metrics",
    "operation:evaluate-retrospective-shortboards",
)

_T = TypeVar("_T", bound=BaseModel)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(payload))


def _load_model(path: Path, model: type[_T]) -> _T | None:
    if not path.is_file():
        return None
    try:
        return model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return None


def _corrupt_gap(*, metric: MetricKey, path: Path, err: Exception) -> MetricCollectionGap:
    return MetricCollectionGap(
        code="artifact_corrupt",
        metric=metric,
        detail=f"{path.name} unparseable: {err}",
    )


def _corrupt_mutation(*, change_id: str, batch_id: str, gap: MetricCollectionGap) -> MutationEvidence:
    return MutationEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="collection_failed",
        value=None,
        killed=0,
        survived=0,
        equivalent=0,
        tested=0,
        selected=0,
        budget_seconds=0,
        elapsed_seconds=0.0,
        collection_gaps=(gap,),
    )


def _corrupt_assertion_strength(
    *, change_id: str, batch_id: str, gap: MetricCollectionGap
) -> AssertionStrengthEvidence:
    return AssertionStrengthEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="collection_failed",
        collection_gaps=(gap,),
    )


def _corrupt_baseline_drift(
    *, change_id: str, batch_id: str, gap: MetricCollectionGap
) -> BaselineDriftEvidence:
    return BaselineDriftEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="collection_failed",
        drift_band=DEFAULT_DRIFT_BAND,
        window=DEFAULT_BASELINE_WINDOW,
        collection_gaps=(gap,),
    )


def _corrupt_adversarial_yield(
    *, change_id: str, batch_id: str, gap: MetricCollectionGap
) -> AdversarialYieldEvidence:
    return AdversarialYieldEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        property="api",
        layer="api",
        sample_count=0,
        counterexample_ids=(),
        unclosed_count=0,
        seed=0,
        status="collection_failed",
        value=None,
        collection_gaps=(gap,),
    )


def _load_nightly_evidence_file(
    path: Path,
    model: type[Any],
    *,
    change_id: str,
    batch_id: str,
    metric: MetricKey,
    corrupt_factory: Any,
) -> Any | None:
    """Load one nightly evidence file; present-but-corrupt → typed stub.

    Absent files stay ``None`` (aggregate maps that to ``pending_nightly``).
    A present but unparseable file must never collapse to that missing path —
    fail-close via ``artifact_corrupt`` / ``collection_failed`` instead.
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


def _risk_from_context(change_dir: Path, pr_metrics: MetricsDocument | None) -> RiskTierResolution:
    if pr_metrics is not None:
        return RiskTierResolution(
            tier=pr_metrics.risk_tier,
            lower_bound=pr_metrics.risk_tier_lower_bound,
            declared=pr_metrics.risk_tier_declared,
            lowered_declarations=pr_metrics.risk_lowered_declarations,
        )
    return resolve_risk_tier(load_case_entries_strict(change_dir))


def _nightly_task(target: str) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id=f"t-{target.removeprefix('operation:')}",
        node_id=target.removeprefix("operation:"),
        graph_id="metrics-nightly-workflow",
        target=target,
        input={"with": {}},
    )


def run_metrics_nightly_graph(
    workspace: TaskWorkspace,
    context: RuntimeContext,
    *,
    mutation_runner: MutationTool | None = None,
) -> list[TaskResult]:
    """Run ``metrics-nightly`` ops in schema order; inject FakeRunner for tests.

    Never writes ``inspect/metrics.json``. Stops on the first non-succeeded
    result so later artifacts (nightly json / shortboards) are not written.
    Production GraphRuntime uses the same operation handlers; this helper is
    the injectable acceptance seam (M2 Task 7).
    """
    ops: dict[str, Any] = {
        "operation:load-latest-pr-metrics": load_latest_pr_metrics_operation,
        "operation:run-mutation-sample": run_mutation_sample_operation,
        "operation:compute-assertion-strength": compute_assertion_strength_operation,
        "operation:compute-baseline-drift": compute_baseline_drift_operation,
        "operation:collect-adversarial-yield": collect_adversarial_yield_operation,
        "operation:materialize-quarantine-projection": materialize_quarantine_projection_operation,
        "operation:materialize-c-layer-metrics": materialize_c_layer_metrics_operation,
        "operation:aggregate-nightly-metrics": aggregate_nightly_metrics_operation,
        "operation:evaluate-retrospective-shortboards": evaluate_retrospective_shortboards_operation,
    }
    results: list[TaskResult] = []
    for target in NIGHTLY_GRAPH_TARGETS:
        task = _nightly_task(target)
        op = ops[target]
        if target == "operation:run-mutation-sample":
            result = op(task, workspace, context, runner=mutation_runner)
        else:
            result = op(task, workspace, context)
        results.append(result)
        if result.status != "succeeded":
            break
    return results


def run_nightly_metrics_pipeline_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    results = run_metrics_nightly_graph(workspace, context)
    failed = next((result for result in results if result.status != "succeeded"), None)
    if failed is not None:
        return failed
    last = results[-1]
    return TaskResult(status="succeeded", value=last.value)


def load_latest_pr_metrics_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Read ``inspect/metrics.json`` if present; never rewrite it."""
    del task  # contract surface only
    change_id = context.change_id or workspace.change_dir.name
    pr_path = workspace.change_dir / PR_METRICS_REL
    present = pr_path.is_file()
    digest = ""
    if present:
        digest = hashlib.sha256(pr_path.read_bytes()).hexdigest()
    _write_json(
        workspace.change_dir / NIGHTLY_SOURCE_REL,
        {
            "change_id": change_id,
            "pr_metrics_rel": PR_METRICS_REL,
            "pr_metrics_present": present,
            "pr_metrics_sha256": digest,
        },
    )
    return TaskResult(
        status="succeeded",
        value={
            "pr_metrics_present": present,
            "pr_metrics_sha256": digest,
            "source_rel": NIGHTLY_SOURCE_REL,
        },
    )


def aggregate_nightly_metrics_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Fold nightly evidence into ``inspect/metrics-nightly.json``; never touch PR."""
    del task
    change_id = context.change_id or workspace.change_dir.name
    try:
        policy = load_policy(context.resolved_host_root)
        digest = compute_policy_digest(policy)
    except Exception as err:  # noqa: BLE001 — surface as invalid_input
        return task_failure("invalid_input", f"cannot load policy digest: {err}")

    pr_metrics = _load_model(workspace.change_dir / PR_METRICS_REL, MetricsDocument)
    try:
        risk = _risk_from_context(workspace.change_dir, pr_metrics)
    except CaseArtifactError as err:
        return task_failure("invalid_input", str(err))
    mutation = _load_nightly_evidence_file(
        workspace.change_dir / MUTATION_EVIDENCE_REL,
        MutationEvidence,
        change_id=change_id,
        batch_id=MUTATION_BATCH_ID,
        metric="mutation_score",
        corrupt_factory=_corrupt_mutation,
    )
    assertion = _load_nightly_evidence_file(
        workspace.change_dir / ASSERTION_STRENGTH_EVIDENCE_REL,
        AssertionStrengthEvidence,
        change_id=change_id,
        batch_id=ASSERTION_STRENGTH_BATCH_ID,
        metric="assertion_strength",
        corrupt_factory=_corrupt_assertion_strength,
    )
    baseline = _load_nightly_evidence_file(
        workspace.change_dir / BASELINE_DRIFT_EVIDENCE_REL,
        BaselineDriftEvidence,
        change_id=change_id,
        batch_id=BASELINE_DRIFT_BATCH_ID,
        metric="baseline_drift",
        corrupt_factory=_corrupt_baseline_drift,
    )
    adversarial = _load_nightly_evidence_file(
        workspace.change_dir / ADVERSARIAL_YIELD_EVIDENCE_REL,
        AdversarialYieldEvidence,
        change_id=change_id,
        batch_id=ADVERSARIAL_YIELD_BATCH_ID,
        metric="adversarial_yield",
        corrupt_factory=_corrupt_adversarial_yield,
    )

    band = policy.evidence_sufficiency.floors[risk.tier]
    document = aggregate_nightly_metrics(
        change_id=change_id,
        computed_at=datetime.now(tz=UTC),
        policy_digest=digest,
        risk=risk,
        mutation=mutation,
        assertion_strength=assertion,
        baseline_drift=baseline,
        adversarial_yield=adversarial,
        floors=band,
        sufficiency=policy.evidence_sufficiency,
    )

    out = workspace.change_dir / NIGHTLY_METRICS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(document))
    return TaskResult(
        status="succeeded",
        value={
            "path": NIGHTLY_METRICS_REL,
            "cadence": "nightly",
            "floor_ratio": document.floor_ratio,
            "shortboard_count": len(document.shortboards),
            "pr_metrics_present": pr_metrics is not None,
        },
    )


def evaluate_retrospective_shortboards_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Expose nightly shortboards + sufficiency verdict without reopening PR verdict.

    Runs ``evaluate_metrics_sufficiency`` on ``inspect/metrics-nightly.json`` so
    the open-counterexample hard rule (``adversarial_clean``) is adjudicated on
    the nightly carrier. Writes only ``inspect/metrics-nightly-shortboards.json``;
    never mutates ``inspect/metrics.json``, quality-gate ``final_status``, or the
    execution manifest (§6 / M3 Task 2).
    """
    del task
    change_id = context.change_id or workspace.change_dir.name
    nightly_path = workspace.change_dir / NIGHTLY_METRICS_REL
    shortboards: list[dict[str, Any]] = []
    floor_ratio: float | None = None
    sufficiency_verdict: str | None = None
    mutation_budget_seconds: int | None = None
    if nightly_path.is_file():
        try:
            raw_doc = json.loads(nightly_path.read_text(encoding="utf-8"))
            document = MetricsDocument.model_validate(raw_doc)
            shortboards = [board.model_dump(mode="json") for board in document.shortboards]
            floor_ratio = document.floor_ratio
            policy = load_policy(context.resolved_host_root)
            decision = evaluate_metrics_sufficiency(document, policy.evidence_sufficiency)
            sufficiency_verdict = decision.verdict
            mutation_budget_seconds = decision.mutation_budget_seconds
            for board in decision.shortboards:
                dumped = board.model_dump(mode="json")
                if dumped not in shortboards:
                    shortboards.append(dumped)
        except (OSError, ValueError, ValidationError):
            # Fall back to a best-effort shortboard extract when the document is
            # unreadable — still never invent a sufficiency pass.
            shortboards = []
            floor_ratio = None
            sufficiency_verdict = None
            mutation_budget_seconds = None
            try:
                payload = json.loads(nightly_path.read_text(encoding="utf-8"))
                raw = payload.get("shortboards")
                if isinstance(raw, list):
                    shortboards = [item for item in raw if isinstance(item, dict)]
                raw_ratio = payload.get("floor_ratio")
                if isinstance(raw_ratio, (int, float)):
                    floor_ratio = float(raw_ratio)
            except (OSError, ValueError):
                pass
    _write_json(
        workspace.change_dir / NIGHTLY_SHORTBOARDS_REL,
        {
            "change_id": change_id,
            "source_rel": NIGHTLY_METRICS_REL,
            "shortboards": shortboards,
            # Named floor_ratio — never "confidence" (§3.10 / §12.14).
            "floor_ratio": floor_ratio,
            "sufficiency_verdict": sufficiency_verdict,
            "mutation_budget_seconds": mutation_budget_seconds,
            "reopens_pr_verdict": False,
            "touches_quality_gate_final_status": False,
            "consumers": ["pr_metrics_batch", "retro"],
        },
    )
    return TaskResult(
        status="succeeded",
        value={
            "path": NIGHTLY_SHORTBOARDS_REL,
            "shortboard_count": len(shortboards),
            "floor_ratio": floor_ratio,
            "sufficiency_verdict": sufficiency_verdict,
            "mutation_budget_seconds": mutation_budget_seconds,
            "reopens_pr_verdict": False,
        },
    )


__all__ = [
    "ADVERSARIAL_YIELD_EVIDENCE_REL",
    "ASSERTION_STRENGTH_EVIDENCE_REL",
    "BASELINE_DRIFT_EVIDENCE_REL",
    "MUTATION_EVIDENCE_REL",
    "NIGHTLY_GRAPH_TARGETS",
    "NIGHTLY_SHORTBOARDS_REL",
    "NIGHTLY_SOURCE_REL",
    "aggregate_nightly_metrics_operation",
    "collect_adversarial_yield_operation",
    "compute_assertion_strength_operation",
    "compute_baseline_drift_operation",
    "evaluate_retrospective_shortboards_operation",
    "load_latest_pr_metrics_operation",
    "run_metrics_nightly_graph",
    "run_mutation_sample_operation",
    "run_nightly_metrics_pipeline_operation",
]
