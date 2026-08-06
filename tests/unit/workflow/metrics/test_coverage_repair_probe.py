"""Task 3: coverage-repair probe — in-memory metrics + same sufficiency table."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import yaml

from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGapKind,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_BRIEF_REL,
    RepairableGapKind,
)
from assurance_agent.artifacts.models.metrics import MetricScope
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    JourneyCoverageEvidence,
)
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import write_batch_evidence
from assurance_agent.workflow.metrics.coverage_repair import (
    build_repair_brief,
    probe_coverage_repair_need_operation,
)
from assurance_agent.workflow.metrics.pr_metrics import (
    INSPECT_METRICS_REL,
    METRICS_SOURCE_BATCH_REL,
    build_metrics_document,
)
from tests.unit.workflow.metrics.test_pr_metrics_materialize import (
    BATCH_NEW,
    CHANGE_ID,
    COMPUTED_AT,
    _seed_project,
    _write_complete_batch,
)


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="probe-coverage-repair-need",
        graph_id="coverage-repair",
        target="operation:probe-coverage-repair-need",
        input={"with": {}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _seed_low_risk(tmp_path: Path) -> tuple[Path, Path]:
    """Reuse materialize helpers, then pin risk band to packaged ``low`` floors."""
    project_root, change_dir = _seed_project(tmp_path)
    cases = change_dir / "cases" / "system" / "api" / "case.yaml"
    cases.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_API_001",
                        "title": "api",
                        "status": "active",
                        "priority": "P2",
                        "severity": "minor",
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


def _add_touched_scopes(change_dir: Path, batch_id: str) -> None:
    """Auth/journey low floors use ``target: touched``; declared-only → reject."""
    write_batch_evidence(
        change_dir,
        batch_id,
        "auth-matrix.json",
        AuthMatrixEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=MetricScope.of(total=1, covered=1),
            touched=MetricScope.of(total=1, covered=1),
            value=1.0,
        ),
    )
    write_batch_evidence(
        change_dir,
        batch_id,
        "journey-coverage.json",
        JourneyCoverageEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=MetricScope.of(total=1, covered=1),
            touched=MetricScope.of(total=1, covered=1),
            value=1.0,
        ),
    )


def _seed_batch(
    change_dir: Path,
    batch_id: str = BATCH_NEW,
    *,
    constraint_covered: int = 2,
) -> None:
    _write_complete_batch(change_dir, batch_id, constraint_covered=constraint_covered)
    _add_touched_scopes(change_dir, batch_id)


def _write_gaps(change_dir: Path, batch_id: str, gaps: list[dict[str, object]]) -> None:
    path = change_dir / COVERAGE_GAPS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = CoverageGapsDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "batch_id": batch_id,
            "projection_digest": "a" * 64,
            "gaps": gaps,
        }
    )
    path.write_text(doc.model_dump_json(indent=2) + "\n", encoding="utf-8")


def test_probe_operation_registered_under_exact_key() -> None:
    ops = default_operations()
    assert ops["operation:probe-coverage-repair-need"] is probe_coverage_repair_need_operation


def test_below_packaged_low_constraint_floor_is_needs_human(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "needs_human"
    assert brief.batch_id == BATCH_NEW
    assert any(s.metric == "constraint_coverage" for s in brief.shortboards)


def test_collection_gap_batch_is_reject_and_ineligible(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=4)
    corrupt = change_dir / "execution" / "runs" / BATCH_NEW / "coverage-diff.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "reject"
    assert brief.eligible is False
    assert brief.repair_items == ()


def test_fully_passing_batch_is_ineligible(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=4)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "pass"
    assert brief.eligible is False
    assert brief.repair_items == ()


def test_no_batch_at_all_is_ineligible_without_exception(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.eligible is False
    assert brief.batch_id is None
    assert brief.probe_verdict == "reject"
    assert brief.repair_items == ()


def test_project_policy_override_changes_probe_verdict(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=2)  # 0.5 clears packaged low min 0.5

    before = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert before.probe_verdict == "pass"

    policy = load_policy(project_root).model_dump(mode="json")
    policy["evidence_sufficiency"]["floors"]["low"]["constraint_coverage"] = {
        "target": "value",
        "min": 0.9,
    }
    (project_root / ".aa" / "policy.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=False),
        encoding="utf-8",
    )

    after = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert after.probe_verdict == "needs_human"


def test_build_repair_brief_writes_no_authoritative_metrics(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)

    build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert not (change_dir / INSPECT_METRICS_REL).exists()
    assert not (change_dir / METRICS_SOURCE_BATCH_REL).exists()


def test_operation_writes_brief_without_authoritative_metrics(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)

    result = probe_coverage_repair_need_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert result.value["probe_verdict"] == "needs_human"
    assert result.value["batch_id"] == BATCH_NEW
    assert (change_dir / COVERAGE_REPAIR_BRIEF_REL).is_file()
    assert (change_dir / "coverage-repair" / "brief.md").is_file()
    assert not (change_dir / INSPECT_METRICS_REL).exists()
    assert not (change_dir / METRICS_SOURCE_BATCH_REL).exists()


def test_probe_verdict_matches_evaluate_metrics_sufficiency(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)
    policy = load_policy(project_root)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    document = build_metrics_document(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        batch_id=BATCH_NEW,
    )
    decision = evaluate_metrics_sufficiency(document, policy.evidence_sufficiency)
    assert brief.probe_verdict == decision.verdict


def test_gap_partitioning_repairable_and_deferred(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)
    gaps: list[dict[str, object]] = [
        {
            "kind": "uncovered_required_case",
            "locator": {"case_id": "TC_API_001"},
            "layer": "execution",
            "batch_id": BATCH_NEW,
        },
        {
            "kind": "stale_required_case",
            "locator": {"case_id": "TC_API_002"},
            "layer": "execution",
            "batch_id": BATCH_NEW,
        },
        {
            "kind": "constraint_without_property",
            "locator": {"constraint_key": "entities.dept.constraints.name_unique"},
            "layer": "execution",
            "batch_id": BATCH_NEW,
        },
        {
            "kind": "matrix_cell_unasserted",
            "locator": {"cell": "dept_list_guest"},
            "layer": "execution",
            "batch_id": BATCH_NEW,
        },
        {
            "kind": "unmapped_test_cluster",
            "locator": {"cluster_key": "tests/api/test_orphan.py"},
            "layer": "execution",
            "batch_id": BATCH_NEW,
        },
        {
            "kind": "uncovered_required_case",
            "locator": {"case_id": "TC_DECL_001"},
            "layer": "declaration",
            "batch_id": BATCH_NEW,
        },
    ]
    _write_gaps(change_dir, BATCH_NEW, gaps)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "needs_human"
    assert brief.eligible is True
    repair_kinds = {item.kind for item in brief.repair_items}
    assert repair_kinds == set(get_args(RepairableGapKind))
    assert len(brief.repair_items) == 4

    deferred_by_reason = {item.reason: item for item in brief.deferred_to_intake}
    assert "unmapped_cluster" in deferred_by_reason
    assert deferred_by_reason["unmapped_cluster"].kind == "unmapped_test_cluster"
    assert "declaration_layer" in deferred_by_reason
    assert deferred_by_reason["declaration_layer"].kind == "uncovered_required_case"
    assert deferred_by_reason["declaration_layer"].locator.case_id == "TC_DECL_001"


def test_missing_coverage_gaps_yields_ineligible(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "needs_human"
    assert brief.eligible is False
    assert brief.repair_items == ()


def test_invalid_coverage_gaps_yields_ineligible(tmp_path: Path) -> None:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)
    path = change_dir / COVERAGE_GAPS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-valid-gaps}\n", encoding="utf-8")

    brief = build_repair_brief(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    assert brief.probe_verdict == "needs_human"
    assert brief.eligible is False
    assert brief.repair_items == ()


def test_all_coverage_gap_kinds_covered_by_partition_fixture() -> None:
    """Fixture kinds in partitioning test stay total over CoverageGapKind."""
    kinds_in_fixture = {
        "uncovered_required_case",
        "stale_required_case",
        "constraint_without_property",
        "matrix_cell_unasserted",
        "unmapped_test_cluster",
    }
    assert kinds_in_fixture == set(get_args(CoverageGapKind))
