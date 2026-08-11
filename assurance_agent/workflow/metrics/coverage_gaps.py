"""``operation:build-coverage-gap-signals`` — fold projection → coverage-gaps.json.

Reads ``inspect/trace-projection.json`` (+ optional ``inspect/trace-sufficiency.json``),
writes ``inspect/coverage-gaps.json`` once. Zero LLM. Graph wiring into the
assurance path is deferred — callable + execution-contract registration is enough
for pin tests; attaching after ``materialize-trace-projection`` is a later step.

Also folds in the batch's A2/A3 evidence (``constraint-coverage.json`` /
``auth-matrix.json``) as :class:`CoverageGapFeedstock` when present. Both were
Phase-1 stub-empty extension kinds (§7.1) with the fold's parameter wired but no
caller ever passing it — a numeric shortfall (e.g. ``constraint_coverage == 0.0``)
carried no gap for dual-source case design to pick up. Best-effort and
degrade-to-empty: a missing or gap-carrying batch file drops the corresponding
feedstock list rather than failing the fold, because these two kinds are
Phase-1 extensions the fold already treats as optional.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGapFeedstock,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    JourneyCoverageEvidence,
)
from assurance_agent.artifacts.models.trace import TraceProjectionLike, load_trace_projection_document
from assurance_agent.artifacts.models.trace_sufficiency import TraceSufficiencyFacts
from assurance_agent.evidence.coverage_gaps import build_coverage_gaps
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.auth_matrix import AUTH_MATRIX_REL
from assurance_agent.workflow.metrics.batch_io import batch_runs_dir
from assurance_agent.workflow.metrics.constraint_coverage import CONSTRAINT_COVERAGE_REL
from assurance_agent.workflow.metrics.journey_coverage import JOURNEY_COVERAGE_REL

TRACE_PROJECTION_REL = "inspect/trace-projection.json"
TRACE_SUFFICIENCY_REL = "inspect/trace-sufficiency.json"


def load_trace_projection(change_dir: Path) -> TraceProjectionLike | None:
    path = Path(change_dir) / TRACE_PROJECTION_REL
    if not path.is_file():
        return None
    try:
        return load_trace_projection_document(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return None


def load_trace_sufficiency(change_dir: Path) -> TraceSufficiencyFacts | None:
    path = Path(change_dir) / TRACE_SUFFICIENCY_REL
    if not path.is_file():
        return None
    try:
        return TraceSufficiencyFacts.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return None


def _load_constraint_gap_feedstock(change_dir: Path, batch_id: str) -> tuple[str, ...]:
    """Uncovered constraint keys from the batch's A2 evidence, or ``()``.

    Prefers the ``touched`` scope (what this change was obligated to cover) over
    ``declared`` (repo-wide): a whole-repo backlog would flood dual-source case
    design with gaps unrelated to the current diff. Falls back to ``declared``
    only when ``touched`` is absent, so a batch predating touched-entity
    derivation still contributes something rather than nothing.
    """
    path = batch_runs_dir(change_dir, batch_id) / CONSTRAINT_COVERAGE_REL
    if not path.is_file():
        return ()
    try:
        evidence = ConstraintCoverageEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return ()
    if evidence.collection_gaps:
        return ()
    scope = evidence.touched if evidence.touched is not None else evidence.declared
    return scope.uncovered if scope is not None else ()


def _load_matrix_gap_feedstock(change_dir: Path, batch_id: str) -> tuple[str, ...]:
    """Declared ``cell_id``s the batch's A3 evidence could not assert, or ``()``."""
    path = batch_runs_dir(change_dir, batch_id) / AUTH_MATRIX_REL
    if not path.is_file():
        return ()
    try:
        evidence = AuthMatrixEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return ()
    if evidence.collection_gaps:
        return ()
    return tuple(sorted(cell.cell_id for cell in evidence.cells if not cell.asserted))


def _load_weak_oracle_gap_feedstock(change_dir: Path, batch_id: str) -> tuple[str, ...]:
    """Case IDs whose A4 journey evidence lacks a classifiable strong oracle.

    Trace projection calls these cases covered because a test is mapped and ran;
    A4 correctly does not.  Without this join the numeric A4 shortfall produces
    no repair item, so the inner loop cannot strengthen the assertion.
    """
    path = batch_runs_dir(change_dir, batch_id) / JOURNEY_COVERAGE_REL
    if not path.is_file():
        return ()
    try:
        evidence = JourneyCoverageEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return ()
    if evidence.collection_gaps:
        return ()
    repairable_statuses = frozenset({"weak_oracle", "oracle_unavailable"})
    return tuple(
        sorted(
            {
                case_id
                for item in evidence.items
                if item.status in repairable_statuses
                for case_id in item.case_ids
            }
        )
    )


def build_coverage_gap_signals(
    *,
    change_dir: Path,
    change_id: str,
) -> CoverageGapsDocument:
    """Fold on-disk projection (+ sufficiency when present) and write once."""
    projection = load_trace_projection(change_dir)
    if projection is None:
        raise FileNotFoundError(TRACE_PROJECTION_REL)
    sufficiency = load_trace_sufficiency(change_dir)
    batch_id = projection.authoritative_batch_id or ""
    if not batch_id:
        batch_id = "unknown"
    feedstock = CoverageGapFeedstock(
        constraints_without_property=_load_constraint_gap_feedstock(change_dir, batch_id),
        matrix_cells_unasserted=_load_matrix_gap_feedstock(change_dir, batch_id),
        cases_without_strong_oracle=_load_weak_oracle_gap_feedstock(change_dir, batch_id),
    )
    document = build_coverage_gaps(
        projection,
        sufficiency,
        change_id=change_id,
        batch_id=batch_id,
        feedstock=feedstock,
    )
    out = Path(change_dir) / COVERAGE_GAPS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(document))
    return document


def build_coverage_gap_signals_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task  # contract-selected; no with-map inputs yet
    change_dir = workspace.change_dir
    change_id = context.change_id or ""
    if not change_id:
        return task_failure("invalid_input", "operation:build-coverage-gap-signals requires change_id")
    try:
        document = build_coverage_gap_signals(change_dir=change_dir, change_id=change_id)
    except FileNotFoundError:
        return task_failure(
            "invalid_input",
            f"missing or corrupt {TRACE_PROJECTION_REL}",
        )
    return TaskResult(
        status="succeeded",
        value={
            "written": True,
            "path": COVERAGE_GAPS_REL,
            "gap_count": len(document.gaps),
            "batch_id": document.batch_id,
            "projection_digest": document.projection_digest,
        },
    )


__all__ = [
    "COVERAGE_GAPS_REL",
    "TRACE_PROJECTION_REL",
    "TRACE_SUFFICIENCY_REL",
    "build_coverage_gap_signals",
    "build_coverage_gap_signals_operation",
    "load_trace_projection",
    "load_trace_sufficiency",
]
