"""``operation:materialize-c-layer-metrics`` — write report-only C-layer once.

Loads feedstock from the change tree (and optional project problems ledger),
folds via ``aggregate_c_layer_metrics``, writes ``inspect/metrics-c-layer.json``.
Not wired into ``metrics-sufficiency-gate`` or ``quality_gate`` — report/retro
only. Missing on-disk inputs stay ``not_evaluated`` per vector.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.c_layer import C_LAYER_METRICS_REL, CLayerMetricsDocument
from assurance_agent.artifacts.models.coverage_gaps import COVERAGE_GAPS_REL, CoverageGapsDocument
from assurance_agent.artifacts.models.discovery import Counterexample
from assurance_agent.artifacts.models.issues import Problem, ProblemProjection
from assurance_agent.artifacts.models.promotion import PromotionReceipt, RegressionCandidate
from assurance_agent.evidence.c_layer_metrics import aggregate_c_layer_metrics
from assurance_agent.workflow.discovery.replay_receipts import load_replay_attempt_receipts
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace

_CE_DIR_REL = "discovery/counterexamples"
_CANDIDATES_DIR_REL = "discovery/candidates"
_PROBLEMS_REL = "qa/issues/problems.json"


class CLayerInputIntegrityError(ValueError):
    """A present C-layer feedstock artifact is corrupt or contradictory."""


def load_c_layer_metrics(change_dir: Path) -> CLayerMetricsDocument | None:
    """Load ``inspect/metrics-c-layer.json`` when present and valid."""
    path = Path(change_dir) / C_LAYER_METRICS_REL
    if not path.is_file():
        return None
    try:
        return CLayerMetricsDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
        raise CLayerInputIntegrityError(f"{C_LAYER_METRICS_REL} is invalid: {err}") from err


def _load_problems(project_root: Path) -> list[Problem] | None:
    path = Path(project_root) / _PROBLEMS_REL
    if not path.is_file():
        return None
    try:
        projection = ProblemProjection.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
        raise CLayerInputIntegrityError(f"{_PROBLEMS_REL} is invalid: {err}") from err
    return list(projection.problems)


def _load_coverage_gaps(
    change_dir: Path,
    rel: str,
    *,
    expected_change_id: str,
) -> CoverageGapsDocument | None:
    path = Path(change_dir) / rel
    if not path.is_file():
        return None
    try:
        document = CoverageGapsDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
        raise CLayerInputIntegrityError(f"{rel} is invalid: {err}") from err
    if document.change_id != expected_change_id:
        raise CLayerInputIntegrityError(f"{rel} change_id={document.change_id!r} != {expected_change_id!r}")
    return document


def _load_promotion_feedstock(
    change_dir: Path,
    *,
    expected_change_id: str,
) -> tuple[list[PromotionReceipt] | None, list[RegressionCandidate] | None, int | None]:
    ce_root = Path(change_dir) / _CE_DIR_REL
    candidates_root = Path(change_dir) / _CANDIDATES_DIR_REL

    total: int | None
    counterexamples: dict[str, Counterexample] | None = None
    if not ce_root.is_dir():
        total = None
    else:
        counterexamples = {}
        ce_paths = sorted(ce_root.glob("*.yaml")) + sorted(ce_root.glob("*.yml"))
        for path in ce_paths:
            rel = path.relative_to(change_dir).as_posix()
            try:
                ce = Counterexample.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
                raise CLayerInputIntegrityError(f"{rel} is invalid: {err}") from err
            if path.stem != ce.counterexample_id:
                raise CLayerInputIntegrityError(
                    f"{rel} counterexample_id={ce.counterexample_id!r} does not match filename"
                )
            if ce.counterexample_id in counterexamples:
                raise CLayerInputIntegrityError(
                    f"duplicate counterexample_id {ce.counterexample_id!r} in {_CE_DIR_REL}"
                )
            counterexamples[ce.counterexample_id] = ce
        total = len(counterexamples)

    if not candidates_root.is_dir():
        if total is None:
            return None, None, None
        return [], [], total

    receipts: list[PromotionReceipt] = []
    candidates: list[RegressionCandidate] = []
    for candidate_dir in sorted(p for p in candidates_root.iterdir() if p.is_dir()):
        cand_path = candidate_dir / "candidate.yaml"
        if not cand_path.is_file():
            rel = candidate_dir.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(f"{rel} is missing candidate.yaml")
        try:
            raw = yaml.safe_load(cand_path.read_text(encoding="utf-8"))
            candidate = RegressionCandidate.model_validate(raw)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(f"{rel} is invalid: {err}") from err
        if candidate.candidate_id != candidate_dir.name:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} candidate_id={candidate.candidate_id!r} does not match directory"
            )
        if candidate.change_id != expected_change_id:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} change_id={candidate.change_id!r} != {expected_change_id!r}"
            )
        counterexample = (
            counterexamples.get(candidate.counterexample_id) if counterexamples is not None else None
        )
        if counterexample is None:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} references unknown counterexample_id {candidate.counterexample_id!r}"
            )
        if counterexample.finding_status != "confirmed":
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} references counterexample {candidate.counterexample_id!r} that is not confirmed"
            )
        if candidate.campaign_id != counterexample.campaign_id:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} campaign_id={candidate.campaign_id!r} does not match counterexample"
            )
        if candidate.oracle_id != counterexample.oracle_id:
            rel = cand_path.relative_to(change_dir).as_posix()
            raise CLayerInputIntegrityError(
                f"{rel} oracle_id={candidate.oracle_id!r} does not match counterexample"
            )
        candidates.append(candidate)
        receipt_path = candidate_dir / "promotion-receipt.json"
        if receipt_path.is_file():
            try:
                receipt = PromotionReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
                rel = receipt_path.relative_to(change_dir).as_posix()
                raise CLayerInputIntegrityError(f"{rel} is invalid: {err}") from err
            if receipt.candidate_id != candidate.candidate_id:
                rel = receipt_path.relative_to(change_dir).as_posix()
                raise CLayerInputIntegrityError(
                    f"{rel} candidate_id={receipt.candidate_id!r} does not match candidate"
                )
            receipts.append(receipt)

    if total is None and not receipts and not candidates:
        return None, None, None
    return receipts, candidates, total


def materialize_c_layer_metrics(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    computed_at: datetime | None = None,
    previous_gaps_rel: str | None = None,
) -> CLayerMetricsDocument:
    """Load feedstock, aggregate, write ``inspect/metrics-c-layer.json`` once."""
    problems = _load_problems(project_root)
    receipts, candidates, total = _load_promotion_feedstock(
        change_dir,
        expected_change_id=change_id,
    )
    current_gaps = _load_coverage_gaps(
        change_dir,
        COVERAGE_GAPS_REL,
        expected_change_id=change_id,
    )
    previous_gaps = (
        _load_coverage_gaps(
            change_dir,
            previous_gaps_rel,
            expected_change_id=change_id,
        )
        if previous_gaps_rel
        else None
    )
    # Replay dir absent → empty tuple (zero denom → not_evaluated), not missing.
    replay_receipts = load_replay_attempt_receipts(change_dir)
    at = computed_at or datetime.now(tz=UTC)
    document = aggregate_c_layer_metrics(
        problems=problems,
        promotion_receipts=receipts,
        candidates=candidates,
        total_counterexamples=total,
        previous_gaps=previous_gaps,
        current_gaps=current_gaps,
        replay_receipts=replay_receipts,
        change_id=change_id,
        computed_at=at,
    )
    out = Path(change_dir) / C_LAYER_METRICS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(document))
    return document


def materialize_c_layer_metrics_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    with_map = task_with(task)
    change_id = context.change_id or ""
    if not change_id:
        return task_failure("invalid_input", "operation:materialize-c-layer-metrics requires change_id")
    previous_gaps_rel = with_map.get("previous_gaps_rel")
    if previous_gaps_rel is not None and not isinstance(previous_gaps_rel, str):
        return task_failure(
            "invalid_input",
            "operation:materialize-c-layer-metrics with.previous_gaps_rel must be a string",
        )
    computed_raw = with_map.get("computed_at") or context.params.get("computed_at")
    computed_at: datetime | None = None
    try:
        if isinstance(computed_raw, str) and computed_raw.strip():
            computed_at = datetime.fromisoformat(computed_raw.replace("Z", "+00:00"))
        document = materialize_c_layer_metrics(
            change_dir=workspace.change_dir,
            project_root=workspace.project_root,
            change_id=change_id,
            computed_at=computed_at,
            previous_gaps_rel=previous_gaps_rel,
        )
    except ValueError as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "written": True,
            "path": C_LAYER_METRICS_REL,
            "escape_rate_status": document.escape_rate.status,
            "promotion_rate_status": document.counterexample_promotion_rate.status,
            "gap_closure_status": document.coverage_gap_closure_rate.status,
            "seed_replay_status": document.seed_replay_stability.status,
        },
    )


__all__ = [
    "CLayerInputIntegrityError",
    "load_c_layer_metrics",
    "materialize_c_layer_metrics",
    "materialize_c_layer_metrics_operation",
]
