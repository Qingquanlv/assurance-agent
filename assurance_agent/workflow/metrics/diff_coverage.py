"""``operation:collect-diff-coverage`` — materialize diff line coverage (§5-A1).

Reads only batch-scoped ``raw/coverage.json`` + ``raw/changed-lines.json``.
Never opens freeze-excluded ``.coverage*`` sidecars outside the batch raw dir.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models.metrics import MetricCollectionGap
from assurance_agent.artifacts.models.pr_metric_evidence import (
    CoverageDiffEvidence,
    CoverageDiffFile,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import (
    batch_raw_dir,
    is_forbidden_coverage_sidecar,
    resolve_batch_id,
    write_batch_evidence,
)

COVERAGE_DIFF_REL = "coverage-diff.json"
COVERAGE_JSON_NAME = "coverage.json"
CHANGED_LINES_NAME = "changed-lines.json"


def collect_diff_coverage(
    *,
    change_id: str,
    batch_id: str,
    coverage_json: Mapping[str, Any] | None,
    changed_lines: Mapping[str, Sequence[int]] | None,
    source: Mapping[str, str] | None = None,
) -> CoverageDiffEvidence:
    """Pure fold: coverage.py JSON × changed-line map → diff coverage evidence."""
    if coverage_json is None:
        return CoverageDiffEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            total_changed_lines=0,
            covered_changed_lines=0,
            value=None,
            files=(),
            source=dict(source or {}),
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="diff_coverage",
                    detail="coverage.json missing under batch raw/",
                ),
            ),
        )

    if changed_lines is None:
        return CoverageDiffEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            total_changed_lines=0,
            covered_changed_lines=0,
            value=None,
            files=(),
            source=dict(source or {}),
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="diff_coverage",
                    detail="changed-lines.json missing under batch raw/",
                ),
            ),
        )

    files_payload = coverage_json.get("files") if isinstance(coverage_json, Mapping) else None
    if not isinstance(files_payload, Mapping):
        return CoverageDiffEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            total_changed_lines=0,
            covered_changed_lines=0,
            value=None,
            files=(),
            source=dict(source or {}),
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="diff_coverage",
                    detail="coverage.json missing files mapping",
                ),
            ),
        )

    file_rows: list[CoverageDiffFile] = []
    total = 0
    covered = 0
    for path, lines in sorted(changed_lines.items()):
        changed = tuple(sorted({int(line) for line in lines if int(line) > 0}))
        if not changed:
            continue
        executed = _executed_lines(files_payload, path)
        covered_lines = tuple(line for line in changed if line in executed)
        uncovered_lines = tuple(line for line in changed if line not in executed)
        total += len(changed)
        covered += len(covered_lines)
        file_rows.append(
            CoverageDiffFile(
                path=path,
                changed_lines=changed,
                covered_lines=covered_lines,
                uncovered_lines=uncovered_lines,
            )
        )

    return CoverageDiffEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        total_changed_lines=total,
        covered_changed_lines=covered,
        value=None if total == 0 else covered / total,
        files=tuple(file_rows),
        source=dict(source or {"coverage_json": f"raw/{COVERAGE_JSON_NAME}"}),
        collection_gaps=(),
    )


def _executed_lines(files_payload: Mapping[str, Any], path: str) -> frozenset[int]:
    entry = files_payload.get(path)
    if entry is None:
        # coverage.py keys are often absolute or cwd-relative; try basename match.
        for key, value in files_payload.items():
            if str(key).endswith(path) or str(key).endswith("/" + path):
                entry = value
                break
    if not isinstance(entry, Mapping):
        return frozenset()
    executed = entry.get("executed_lines")
    if not isinstance(executed, list):
        return frozenset()
    return frozenset(int(line) for line in executed if isinstance(line, int) or str(line).isdigit())


def collect_diff_coverage_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Publish ``execution/runs/<batch>/coverage-diff.json``."""
    change_dir = workspace.change_dir
    project_root = workspace.project_root
    try:
        batch_id = resolve_batch_id(
            change_dir,
            explicit=str(task_with(task).get("batch_id") or "") or None,
        )
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot resolve batch_id: {err}")

    raw_dir = batch_raw_dir(change_dir, batch_id)
    cov_path = raw_dir / COVERAGE_JSON_NAME
    # Explicit refusal: never open freeze-excluded sidecars even if requested via with.
    requested = task_with(task).get("coverage_data_path")
    if isinstance(requested, str) and requested:
        requested_path = Path(requested)
        if not requested_path.is_absolute():
            requested_path = project_root / requested_path
        if is_forbidden_coverage_sidecar(requested_path, allowed_raw_dir=raw_dir):
            evidence = CoverageDiffEvidence(
                schema_version="1",
                change_id=context.change_id,
                batch_id=batch_id,
                total_changed_lines=0,
                covered_changed_lines=0,
                value=None,
                collection_gaps=(
                    MetricCollectionGap(
                        code="collection_failed",
                        metric="diff_coverage",
                        detail=(
                            f"refusing freeze-excluded coverage sidecar {requested!r}; "
                            f"use execution/runs/<batch>/raw/{COVERAGE_JSON_NAME}"
                        ),
                    ),
                ),
            )
            write_batch_evidence(change_dir, batch_id, COVERAGE_DIFF_REL, evidence)
            return TaskResult(
                status="succeeded",
                value={
                    "change_id": context.change_id,
                    "batch_id": batch_id,
                    "written": True,
                    "path": f"execution/runs/{batch_id}/{COVERAGE_DIFF_REL}",
                    "collection_gaps": len(evidence.collection_gaps),
                },
            )

    coverage_json: dict[str, Any] | None
    if cov_path.is_file():
        try:
            loaded = json.loads(cov_path.read_text(encoding="utf-8"))
            coverage_json = loaded if isinstance(loaded, dict) else None
            if coverage_json is None:
                raise ValueError("coverage.json root must be an object")
        except (OSError, json.JSONDecodeError, ValueError) as err:
            evidence = CoverageDiffEvidence(
                schema_version="1",
                change_id=context.change_id,
                batch_id=batch_id,
                total_changed_lines=0,
                covered_changed_lines=0,
                value=None,
                collection_gaps=(
                    MetricCollectionGap(
                        code="artifact_corrupt",
                        metric="diff_coverage",
                        detail=f"cannot parse raw/coverage.json: {err}",
                    ),
                ),
            )
            write_batch_evidence(change_dir, batch_id, COVERAGE_DIFF_REL, evidence)
            return TaskResult(
                status="succeeded",
                value={
                    "change_id": context.change_id,
                    "batch_id": batch_id,
                    "written": True,
                    "path": f"execution/runs/{batch_id}/{COVERAGE_DIFF_REL}",
                    "collection_gaps": 1,
                },
            )
    else:
        coverage_json = None

    changed_path = raw_dir / CHANGED_LINES_NAME
    changed_lines: dict[str, list[int]] | None
    if not changed_path.is_file():
        changed_lines = None
    else:
        try:
            raw_changed = json.loads(changed_path.read_text(encoding="utf-8"))
            changed_lines = {}
            if isinstance(raw_changed, dict):
                for path, lines in raw_changed.items():
                    if isinstance(lines, list):
                        changed_lines[str(path)] = [int(line) for line in lines]
            else:
                raise ValueError("changed-lines.json root must be an object")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as err:
            evidence = CoverageDiffEvidence(
                schema_version="1",
                change_id=context.change_id,
                batch_id=batch_id,
                total_changed_lines=0,
                covered_changed_lines=0,
                value=None,
                collection_gaps=(
                    MetricCollectionGap(
                        code="artifact_corrupt",
                        metric="diff_coverage",
                        detail=f"cannot parse raw/changed-lines.json: {err}",
                    ),
                ),
            )
            write_batch_evidence(change_dir, batch_id, COVERAGE_DIFF_REL, evidence)
            return TaskResult(
                status="succeeded",
                value={
                    "change_id": context.change_id,
                    "batch_id": batch_id,
                    "written": True,
                    "path": f"execution/runs/{batch_id}/{COVERAGE_DIFF_REL}",
                    "collection_gaps": 1,
                },
            )

    evidence = collect_diff_coverage(
        change_id=context.change_id,
        batch_id=batch_id,
        coverage_json=coverage_json,
        changed_lines=changed_lines,
        source={
            "coverage_json": f"raw/{COVERAGE_JSON_NAME}",
            "changed_lines": f"raw/{CHANGED_LINES_NAME}",
        },
    )
    write_batch_evidence(change_dir, batch_id, COVERAGE_DIFF_REL, evidence)
    return TaskResult(
        status="succeeded",
        value={
            "change_id": context.change_id,
            "batch_id": batch_id,
            "written": True,
            "path": f"execution/runs/{batch_id}/{COVERAGE_DIFF_REL}",
            "value": evidence.value,
            "collection_gaps": len(evidence.collection_gaps),
        },
    )


__all__ = [
    "CHANGED_LINES_NAME",
    "COVERAGE_DIFF_REL",
    "COVERAGE_JSON_NAME",
    "collect_diff_coverage",
    "collect_diff_coverage_operation",
]
