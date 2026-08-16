"""Task 7: collect-pr-metrics-batch + materialize-pr-metrics (single authoritative write)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
import pytest

from assurance_agent.artifacts.models.metrics import MetricScope, MetricsDocument
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    PerfSlackEvidence,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import write_batch_evidence
from assurance_agent.workflow.metrics.pr_metrics import (
    INSPECT_METRICS_REL,
    METRICS_SOURCE_BATCH_REL,
    PR_EVIDENCE_FILES,
    build_metrics_document,
    collect_pr_metrics_batch_operation,
    is_complete_batch,
    materialize_pr_metrics,
    materialize_pr_metrics_operation,
    select_latest_complete_batch,
)
from assurance_agent.workflow.metrics.case_inputs import CaseArtifactError
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-MAT-001"
BATCH_OLD = "20260805-090000"
BATCH_NEW = "20260805-110000"
DIGEST = "b" * 64
COMPUTED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task(target: str, **with_fields: object) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id=target.removeprefix("operation:"),
        graph_id="assurance",
        target=target,
        input={"with": with_fields},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    cases = change_dir / "cases" / "system" / "api"
    cases.mkdir(parents=True)
    (cases / "case.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_API_001",
                        "title": "api",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "API",
                        "module": "system.api",
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    return project_root, change_dir


def _write_complete_batch(
    change_dir: Path,
    batch_id: str,
    *,
    covered_changed_lines: int = 2,
    total_changed_lines: int = 4,
    constraint_covered: int = 2,
) -> Path:
    batch = change_dir / "execution" / "runs" / batch_id
    batch.mkdir(parents=True, exist_ok=True)
    diff_value = None if total_changed_lines == 0 else covered_changed_lines / total_changed_lines
    write_batch_evidence(
        change_dir,
        batch_id,
        "coverage-diff.json",
        CoverageDiffEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            total_changed_lines=total_changed_lines,
            covered_changed_lines=covered_changed_lines,
            value=diff_value,
        ),
    )
    declared = MetricScope.of(
        total=4, covered=constraint_covered, uncovered=tuple(f"k{i}" for i in range(constraint_covered, 4))
    )
    write_batch_evidence(
        change_dir,
        batch_id,
        "constraint-coverage.json",
        ConstraintCoverageEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=declared,
            value=declared.value,
        ),
    )
    auth_declared = MetricScope.of(total=1, covered=1)
    write_batch_evidence(
        change_dir,
        batch_id,
        "auth-matrix.json",
        AuthMatrixEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=auth_declared,
            value=1.0,
        ),
    )
    journey_declared = MetricScope.of(total=1, covered=1)
    write_batch_evidence(
        change_dir,
        batch_id,
        "journey-coverage.json",
        JourneyCoverageEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=journey_declared,
            value=1.0,
        ),
    )
    write_batch_evidence(
        change_dir,
        batch_id,
        "perf-slack.json",
        PerfSlackEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            value=3.0,
        ),
    )
    return batch


def test_pr_metrics_operations_registered_under_exact_keys() -> None:
    ops = default_operations()
    assert ops["operation:collect-pr-metrics-batch"] is collect_pr_metrics_batch_operation
    assert ops["operation:materialize-pr-metrics"] is materialize_pr_metrics_operation
    assert "operation:run-tests-and-collect-pr-metrics" in ops


def test_complete_batch_requires_all_five_evidence_files(tmp_path: Path) -> None:
    _, change_dir = _seed_project(tmp_path)
    batch = change_dir / "execution" / "runs" / BATCH_NEW
    batch.mkdir(parents=True)
    assert not is_complete_batch(batch)
    _write_complete_batch(change_dir, BATCH_NEW)
    assert is_complete_batch(batch)
    assert set(PR_EVIDENCE_FILES) == {
        "coverage-diff.json",
        "constraint-coverage.json",
        "auth-matrix.json",
        "journey-coverage.json",
        "perf-slack.json",
    }


def test_select_latest_complete_batch_ignores_incomplete(tmp_path: Path) -> None:
    _, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=1)
    incomplete = change_dir / "execution" / "runs" / BATCH_NEW
    incomplete.mkdir(parents=True)
    (incomplete / "coverage-diff.json").write_text("{}", encoding="utf-8")
    assert select_latest_complete_batch(change_dir) == BATCH_OLD
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3)
    assert select_latest_complete_batch(change_dir) == BATCH_NEW


def test_materialize_writes_inspect_metrics_from_latest_complete_batch(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=1, constraint_covered=1)
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3, constraint_covered=4)

    result = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert result.written is True
    assert result.batch_id == BATCH_NEW
    out = change_dir / "inspect" / "metrics.json"
    assert out.is_file()
    doc = MetricsDocument.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert doc.metrics["diff_coverage"].value == 0.75
    assert doc.metrics["constraint_coverage"].declared is not None
    assert doc.metrics["constraint_coverage"].declared.covered == 4
    assert doc.cadence == "pr"
    assert doc.policy_digest == DIGEST


def test_older_batch_does_not_overwrite_newer_inspect_metrics(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=1)
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3)

    first = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert first.written is True
    assert first.batch_id == BATCH_NEW
    before = (change_dir / "inspect" / "metrics.json").read_bytes()

    # Explicit older batch_id is refused (always-latest); bytes stay put.
    second = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 5, 13, 0, tzinfo=UTC),
        policy_digest=DIGEST,
        batch_id=BATCH_OLD,
    )
    assert second.written is False
    assert second.reason == "older_batch_refused"
    after = (change_dir / "inspect" / "metrics.json").read_bytes()
    assert after == before
    doc = MetricsDocument.model_validate(json.loads(after))
    assert doc.metrics["diff_coverage"].value == 0.75


def test_explicit_older_batch_refused_when_newer_complete_exists(tmp_path: Path) -> None:
    """Always-latest: explicit older batch_id cannot override a newer complete batch."""
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=1)
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3)

    result = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
        batch_id=BATCH_OLD,
    )
    assert result.written is False
    assert result.reason == "older_batch_refused"
    assert result.batch_id == BATCH_OLD
    assert not (change_dir / "inspect" / "metrics.json").exists()


def test_unparseable_complete_batch_file_is_artifact_corrupt_not_missing(
    tmp_path: Path,
) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW)
    corrupt = change_dir / "execution" / "runs" / BATCH_NEW / "coverage-diff.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    assert is_complete_batch(corrupt.parent)

    result = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert result.written is True
    assert result.document is not None
    entry = result.document.metrics["diff_coverage"]
    assert entry.status == "collection_failed"
    gaps = [g for g in result.document.collection_gaps if g.metric == "diff_coverage"]
    assert gaps
    assert gaps[0].code == "artifact_corrupt"
    assert "missing" not in gaps[0].detail.lower()


def test_collect_pr_metrics_batch_writes_only_under_batch_runs(tmp_path: Path) -> None:
    """Bundle runs all five collectors; writes stay under execution/runs/<batch>/."""
    project_root, change_dir = _seed_project(tmp_path)
    batch = change_dir / "execution" / "runs" / BATCH_NEW
    (batch / "raw").mkdir(parents=True)
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_NEW,
                "executed_at": "2026-08-05T11:00:00+00:00",
                "selected_targets": {
                    "api": True,
                    "e2e": False,
                    "fuzz": False,
                    "performance": False,
                },
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    # Plant an inspect marker that collect must never touch.
    inspect_metrics = change_dir / "inspect" / "metrics.json"
    inspect_metrics.parent.mkdir(parents=True)
    planted = b'{"planted":true,"do_not_touch":1}\n'
    inspect_metrics.write_bytes(planted)

    before_paths = {
        path.relative_to(change_dir).as_posix() for path in change_dir.rglob("*") if path.is_file()
    }

    result = collect_pr_metrics_batch_operation(
        _task("operation:collect-pr-metrics-batch", batch_id=BATCH_NEW),
        _workspace(project_root),
        _context(project_root),
    )
    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert result.value.get("batch_id") == BATCH_NEW

    after_paths = {
        path.relative_to(change_dir).as_posix() for path in change_dir.rglob("*") if path.is_file()
    }
    created = after_paths - before_paths
    batch_prefix = f"execution/runs/{BATCH_NEW}/"
    assert created
    assert all(rel.startswith(batch_prefix) for rel in created)
    for name in PR_EVIDENCE_FILES:
        assert (batch / name).is_file()
    assert inspect_metrics.read_bytes() == planted
    assert "inspect/metrics.json" not in created


def test_second_materialize_of_same_latest_is_idempotent_no_rewrite(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3)

    first = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert first.written is True
    before = (change_dir / "inspect" / "metrics.json").read_bytes()

    second = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        policy_digest=DIGEST,
    )
    assert second.written is False
    assert second.reason == "already_materialized"
    assert (change_dir / "inspect" / "metrics.json").read_bytes() == before


def test_materialize_repairs_missing_source_marker_after_authoritative_publish(
    tmp_path: Path,
) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=3)
    first = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert first.written is True
    authoritative = change_dir / "inspect" / "metrics.json"
    before = authoritative.read_bytes()
    marker = change_dir / "inspect" / "metrics-source-batch.json"
    marker.unlink()

    recovered = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        policy_digest=DIGEST,
    )

    assert recovered.written is False
    assert recovered.reason == "source_batch_recovered"
    assert json.loads(marker.read_text(encoding="utf-8"))["batch_id"] == BATCH_NEW
    assert authoritative.read_bytes() == before


def test_missing_source_marker_recovers_historical_batch_before_reporting_stale(
    tmp_path: Path,
) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=3)
    first = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert first.written is True
    authoritative = change_dir / "inspect" / "metrics.json"
    before = authoritative.read_bytes()
    marker = change_dir / "inspect" / "metrics-source-batch.json"
    marker.unlink()
    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=1)

    recovered = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        policy_digest=DIGEST,
    )

    assert recovered.written is False
    assert recovered.reason == "stale_source_batch"
    assert recovered.batch_id == BATCH_OLD
    assert json.loads(marker.read_text(encoding="utf-8"))["batch_id"] == BATCH_OLD
    assert authoritative.read_bytes() == before


def test_malformed_case_yaml_fails_closed_instead_of_resolving_low_risk(
    tmp_path: Path,
) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW)
    case_path = change_dir / "cases" / "system" / "api" / "case.yaml"
    case_path.write_text("schema_version: '1.0'\nadded: [not-a-case]\n", encoding="utf-8")

    with pytest.raises(CaseArtifactError, match="case.yaml"):
        materialize_pr_metrics(
            change_dir=change_dir,
            project_root=project_root,
            change_id=CHANGE_ID,
            computed_at=COMPUTED_AT,
            policy_digest=DIGEST,
        )

    assert not (change_dir / "inspect" / "metrics.json").exists()


def test_materialize_operation_writes_once(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW)
    (change_dir / "execution").mkdir(exist_ok=True)
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_NEW,
                "executed_at": "2026-08-05T11:00:00+00:00",
                "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )

    result = materialize_pr_metrics_operation(
        _task("operation:materialize-pr-metrics"),
        _workspace(project_root),
        _context(project_root),
    )
    assert result.status == "succeeded"
    assert (change_dir / "inspect" / "metrics.json").is_file()

    again = materialize_pr_metrics_operation(
        _task("operation:materialize-pr-metrics"),
        _workspace(project_root),
        _context(project_root),
    )
    assert again.status == "succeeded"
    assert isinstance(again.value, dict)
    assert again.value.get("written") is False


def test_newer_complete_batch_after_materialize_is_refused_not_reported_clean(
    tmp_path: Path,
) -> None:
    """A write-once document that the evidence has moved past must not be adjudicated."""
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_OLD, covered_changed_lines=3)
    first = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )
    assert first.written is True

    _write_complete_batch(change_dir, BATCH_NEW, covered_changed_lines=1)

    stale = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=DIGEST,
    )

    assert stale.written is False
    assert stale.reason == "stale_source_batch"
    assert stale.batch_id == BATCH_OLD

    result = materialize_pr_metrics_operation(
        _task("operation:materialize-pr-metrics"),
        _workspace(project_root),
        _context(project_root),
    )

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert BATCH_NEW in (result.error or "")


def test_build_metrics_document_writes_nothing(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    batch_id = BATCH_NEW
    _write_complete_batch(change_dir, batch_id)

    document = build_metrics_document(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 6, tzinfo=UTC),
        batch_id=batch_id,
        policy_digest=DIGEST,
    )
    # MetricsDocument has no batch_id field; identity is enforced via
    # expected_batch_id during aggregate (no identity_mismatch gaps).
    assert document.change_id == CHANGE_ID
    assert document.metrics["diff_coverage"].value == 0.5
    assert not any(g.code == "identity_mismatch" for g in document.collection_gaps)
    assert not (change_dir / INSPECT_METRICS_REL).exists()
    assert not (change_dir / METRICS_SOURCE_BATCH_REL).exists()


def test_builder_matches_materialized_document(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_complete_batch(change_dir, BATCH_NEW)
    fixed = datetime(2026, 8, 6, tzinfo=UTC)

    built = build_metrics_document(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=fixed,
        batch_id=BATCH_NEW,
        policy_digest=DIGEST,
    )
    result = materialize_pr_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=fixed,
        policy_digest=DIGEST,
        batch_id=BATCH_NEW,
    )
    assert result.document is not None
    assert result.document.model_dump(mode="json") == built.model_dump(mode="json")
