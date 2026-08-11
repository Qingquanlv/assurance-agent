"""``operation:compute-auth-matrix`` — declared cells × parameterized execution (§5-A3)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from assurance_agent.artifacts.models.data_knowledge import AuthMatrixCell, DataKnowledge
from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricScope
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixCellResult,
    AuthMatrixEvidence,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import (
    batch_runs_dir,
    resolve_batch_id,
    write_batch_evidence,
)

AUTH_MATRIX_REL = "auth-matrix.json"
DATA_KNOWLEDGE_REL = ".aa/data-knowledge.yaml"
_CASE_LIST_KEYS = ("added", "modified", "cases")


@dataclass(frozen=True)
class AuthCellExecution:
    """One parameterized execution that asserts a route × method × token cell."""

    route: str
    method: str
    token: str
    outcome: str  # passed|failed|skipped
    actual_status_code: int | None = None
    parameterized_id: str = ""


def compute_auth_matrix(
    *,
    change_id: str,
    batch_id: str,
    cells: Mapping[str, AuthMatrixCell],
    executions: Sequence[AuthCellExecution],
    touched_identities: frozenset[tuple[str, str, str]] | None = None,
) -> AuthMatrixEvidence:
    """Join declared auth_matrix cells with unique route/method/token executions."""
    if not cells:
        return AuthMatrixEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="auth_matrix_coverage",
                    detail="auth_matrix is empty — nothing declared to measure",
                ),
            ),
        )

    by_identity: dict[tuple[str, str, str], AuthCellExecution] = {}
    for execution in executions:
        identity = (execution.route, execution.method, execution.token)
        prior = by_identity.get(identity)
        if prior is None or _rank(execution.outcome) > _rank(prior.outcome):
            by_identity[identity] = execution

    results: list[AuthMatrixCellResult] = []
    covered_ids: list[str] = []
    for cell_id, cell in sorted(cells.items()):
        identity = (cell.route, cell.method, cell.token)
        execution = by_identity.get(identity)
        if execution is None:
            results.append(
                AuthMatrixCellResult(
                    cell_id=cell_id,
                    route=cell.route,
                    method=cell.method,
                    token=cell.token,
                    expected=cell.expected,
                    allowed_status_codes=tuple(cell.allowed_status_codes),
                    asserted=False,
                    outcome="missing",
                )
            )
            continue
        # Status-code proof is required: None must not vacuously match allowed codes.
        if execution.outcome == "passed" and execution.actual_status_code is None:
            asserted = False
            outcome: str = "unasserted"
        else:
            status_ok = (
                execution.actual_status_code is not None
                and execution.actual_status_code in cell.allowed_status_codes
            )
            asserted = execution.outcome == "passed" and status_ok
            outcome = execution.outcome if execution.outcome in {"passed", "failed", "skipped"} else "missing"
        if asserted:
            covered_ids.append(cell_id)
        results.append(
            AuthMatrixCellResult(
                cell_id=cell_id,
                route=cell.route,
                method=cell.method,
                token=cell.token,
                expected=cell.expected,
                allowed_status_codes=tuple(cell.allowed_status_codes),
                asserted=asserted,
                outcome=outcome,  # type: ignore[arg-type]
                actual_status_code=execution.actual_status_code,
                parameterized_id=execution.parameterized_id,
            )
        )

    all_ids = tuple(sorted(cells))
    covered = frozenset(covered_ids)
    declared = MetricScope.of(
        total=len(all_ids),
        covered=len(covered),
        uncovered=tuple(sorted(set(all_ids) - covered)),
    )

    touched: MetricScope | None
    if touched_identities is None:
        touched = None
    else:
        touched_ids = {
            cell_id
            for cell_id, cell in cells.items()
            if (cell.route, cell.method, cell.token) in touched_identities
        }
        touched_covered = covered & touched_ids
        touched = MetricScope.of(
            total=len(touched_ids),
            covered=len(touched_covered),
            uncovered=tuple(sorted(touched_ids - touched_covered)),
        )

    return AuthMatrixEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=declared,
        touched=touched,
        value=declared.value,
        cells=tuple(results),
        collection_gaps=(),
    )


def _rank(outcome: str) -> int:
    return {"passed": 3, "failed": 2, "skipped": 1}.get(outcome, 0)


def compute_auth_matrix_operation(
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

    dk_path = project_root / DATA_KNOWLEDGE_REL
    if not dk_path.is_file():
        evidence = AuthMatrixEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="auth_matrix_coverage",
                    detail="missing .aa/data-knowledge.yaml",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, AUTH_MATRIX_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    try:
        knowledge = DataKnowledge.model_validate(yaml.safe_load(dk_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as err:
        evidence = AuthMatrixEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="auth_matrix_coverage",
                    detail=f"cannot load data-knowledge: {err}",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, AUTH_MATRIX_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    executions: list[AuthCellExecution] = []
    raw_exec = batch_runs_dir(change_dir, batch_id) / "raw" / "auth-matrix-executions.json"
    if raw_exec.is_file():
        try:
            payload = json.loads(raw_exec.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    executions.append(
                        AuthCellExecution(
                            route=str(row["route"]),
                            method=str(row["method"]),
                            token=str(row["token"]),
                            outcome=str(row.get("outcome", "passed")),
                            actual_status_code=(
                                int(row["actual_status_code"])
                                if row.get("actual_status_code") is not None
                                else None
                            ),
                            parameterized_id=str(row.get("parameterized_id", "")),
                        )
                    )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
            evidence = AuthMatrixEvidence(
                schema_version="1",
                change_id=context.change_id,
                batch_id=batch_id,
                declared=None,
                collection_gaps=(
                    MetricCollectionGap(
                        code="artifact_corrupt",
                        metric="auth_matrix_coverage",
                        detail=f"cannot parse auth-matrix-executions.json: {err}",
                    ),
                ),
            )
            write_batch_evidence(change_dir, batch_id, AUTH_MATRIX_REL, evidence)
            return _ok(context.change_id, batch_id, evidence)

    evidence = compute_auth_matrix(
        change_id=context.change_id,
        batch_id=batch_id,
        cells=knowledge.auth_matrix,
        executions=executions,
        touched_identities=_touched_identities(knowledge.auth_matrix, _touched_entities(change_dir)),
    )
    write_batch_evidence(change_dir, batch_id, AUTH_MATRIX_REL, evidence)
    return _ok(context.change_id, batch_id, evidence)


def _touched_entities(change_dir: Path) -> frozenset[str]:
    """Trailing segment of every ``module`` this change's cases declare.

    ``system.dept`` reads as the ``dept`` surface. The change's own cases are the
    only machine-readable statement of what it touches that this operation has —
    the product diff names files, not routes.
    """
    cases_dir = change_dir / "cases"
    if not cases_dir.is_dir():
        return frozenset()
    entities: set[str] = set()
    for path in sorted(cases_dir.rglob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in _CASE_LIST_KEYS:
            entries = doc.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                module = str(entry.get("module") or "").strip()
                if module:
                    entities.add(module.rsplit(".", 1)[-1])
    return frozenset(entities)


def _touched_identities(
    cells: Mapping[str, AuthMatrixCell],
    entities: frozenset[str],
) -> frozenset[tuple[str, str, str]]:
    """Declared cells whose route carries a touched entity as a path segment.

    Always returned, empty included: an empty ``touched`` scope states "this
    change touched no declared cell", which the absent scope cannot distinguish
    from "nobody computed one".
    """
    if not entities:
        return frozenset()
    return frozenset(
        (cell.route, cell.method, cell.token)
        for cell in cells.values()
        if entities & {segment for segment in cell.route.split("/") if segment}
    )


def _ok(change_id: str, batch_id: str, evidence: AuthMatrixEvidence) -> TaskResult:
    return TaskResult(
        status="succeeded",
        value={
            "change_id": change_id,
            "batch_id": batch_id,
            "written": True,
            "path": f"execution/runs/{batch_id}/{AUTH_MATRIX_REL}",
            "value": evidence.value,
            "collection_gaps": len(evidence.collection_gaps),
        },
    )


__all__ = [
    "AUTH_MATRIX_REL",
    "AuthCellExecution",
    "compute_auth_matrix",
    "compute_auth_matrix_operation",
]
