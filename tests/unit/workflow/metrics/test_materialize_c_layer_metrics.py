"""operation:materialize-c-layer-metrics — thin write of report-only C-layer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.c_layer import C_LAYER_METRICS_REL, CLayerMetricsDocument
from assurance_agent.artifacts.models.coverage_gaps import COVERAGE_GAPS_REL, CoverageGapsDocument
from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.issues import ProblemProjection
from assurance_agent.workflow.discovery.replay_receipts import write_replay_attempt_receipts
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.c_layer import materialize_c_layer_metrics_operation
from tests.unit.artifacts.test_models_issues import make_problem

CHANGE_ID = "CH-CLAYER-OP-001"
COMPUTED = "2026-08-05T15:00:00+00:00"


def _counterexample_payload(counterexample_id: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "counterexample_id": counterexample_id,
        "campaign_id": "CAMP-1",
        "round_id": "R1",
        "surface": "api",
        "technique": "boundary",
        "obligation_ids": [],
        "oracle_id": "ORACLE-1",
        "oracle_kind": "search_heuristic",
        "environment_digest": "env1",
        "generated_file_digests": {},
        "setup": {},
        "actions": [],
        "observed": {},
        "expected": {},
        "seed": 42,
        "minimization": {"status": "raw", "parent_counterexample_id": None},
        "replay": {"attempts": 0, "reproduced": 0, "artifact_refs": []},
        "finding_status": "needs_review",
    }


def _candidate_payload(
    *,
    candidate_id: str = "RC-001",
    change_id: str = CHANGE_ID,
    counterexample_id: str = "CE-001",
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "candidate_id": candidate_id,
        "change_id": change_id,
        "campaign_id": "CAMP-1",
        "counterexample_id": counterexample_id,
        "problem_id": None,
        "oracle_id": "ORACLE-1",
        "surface": "api",
        "proposed_targets": ["tests/api/test_regression.py"],
        "source_files": {f"discovery/candidates/{candidate_id}/files/test_regression.py": "sha256:abc"},
        "minimization_status": "raw",
        "purpose": "regression",
        "evidence_refs": [],
    }


def _confirmed_counterexample_payload(counterexample_id: str) -> dict[str, object]:
    payload = _counterexample_payload(counterexample_id)
    payload.update(
        {
            "oracle_kind": "hard_oracle",
            "finding_status": "confirmed",
            "replay": {"attempts": 1, "reproduced": 1, "artifact_refs": []},
        }
    )
    return payload


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task(**with_fields: object) -> ExecutableTask:
    payload = {"computed_at": COMPUTED, **with_fields}
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="materialize-c-layer-metrics",
        graph_id="assurance",
        target="operation:materialize-c-layer-metrics",
        input={"with": payload},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def test_default_operations_registers_materialize_c_layer() -> None:
    assert "operation:materialize-c-layer-metrics" in default_operations()


def test_materialize_writes_document_with_evaluated_c3(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    write_replay_attempt_receipts(
        change_dir,
        (
            ReplayAttemptReceipt(
                schema_version="1",
                counterexample_id="CE-001",
                seed=1,
                attempt_index=0,
                base_revision="deadbeef",
                oracle_set_digest="sha256:" + ("a" * 64),
                outcome="violate",
                observed_digest="sha256:" + ("b" * 64),
            ),
        ),
    )
    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))
    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert result.value["path"] == C_LAYER_METRICS_REL
    assert result.value["seed_replay_status"] == "evaluated"
    assert result.value["escape_rate_status"] == "not_evaluated"
    doc = CLayerMetricsDocument.model_validate_json(
        (change_dir / C_LAYER_METRICS_REL).read_text(encoding="utf-8")
    )
    assert doc.seed_replay_stability.rate == 1.0
    assert doc.escape_rate.status == "not_evaluated"


def test_materialize_loads_problems_and_gap_pair(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    problems_path = tmp_path / "qa" / "issues" / "problems.json"
    problems_path.parent.mkdir(parents=True)
    from assurance_agent.artifacts.models.issues import Problem

    projection = ProblemProjection(
        schema_version="1.0",
        generated_at="2026-08-05T12:00:00Z",
        problems=[
            Problem.model_validate(
                make_problem(
                    escape_analysis={
                        "is_escape": True,
                        "authority": "human_confirmed",
                        "confirmed_at": "2026-08-05T12:00:00Z",
                        "confirmed_by": "qa",
                    }
                )
            )
        ],
    )
    problems_path.write_text(
        projection.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    prev = CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="b1",
        projection_digest="sha256:" + ("1" * 64),
        gaps=(),
    )
    # empty previous → gap closure not_evaluated; still writes current
    prev_rel = "inspect/coverage-gaps.prev.json"
    (change_dir / "inspect").mkdir(parents=True)
    (change_dir / prev_rel).write_bytes(canonical_json_bytes(prev))
    current = CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="b2",
        projection_digest="sha256:" + ("2" * 64),
        gaps=(),
    )
    (change_dir / COVERAGE_GAPS_REL).write_bytes(canonical_json_bytes(current))

    result = materialize_c_layer_metrics_operation(
        _task(computed_at=COMPUTED, previous_gaps_rel=prev_rel),
        _workspace(tmp_path),
        _context(tmp_path),
    )
    assert result.status == "succeeded"
    doc = CLayerMetricsDocument.model_validate_json(
        (change_dir / C_LAYER_METRICS_REL).read_text(encoding="utf-8")
    )
    assert doc.escape_rate.status == "evaluated"
    assert doc.escape_rate.rate == 1.0
    assert doc.coverage_gap_closure_rate.status == "not_evaluated"
    assert doc.computed_at == datetime(2026, 8, 5, 15, 0, tzinfo=UTC)


def test_materialize_counts_counterexamples_for_promotion_denom(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True)
    for i in range(3):
        (ce_dir / f"CE-{i:03d}.yaml").write_text(
            yaml.safe_dump(_counterexample_payload(f"CE-{i:03d}")),
            encoding="utf-8",
        )
    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))
    assert result.status == "succeeded"
    doc = CLayerMetricsDocument.model_validate_json(
        (change_dir / C_LAYER_METRICS_REL).read_text(encoding="utf-8")
    )
    # No applied receipts → 0/3 evaluated (denom present).
    assert doc.counterexample_promotion_rate.status == "evaluated"
    assert doc.counterexample_promotion_rate.numerator == 0
    assert doc.counterexample_promotion_rate.denominator == 3
    assert doc.counterexample_promotion_rate.rate == 0.0


def test_materialize_fails_closed_on_corrupt_problem_projection(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    problems = tmp_path / "qa" / "issues" / "problems.json"
    problems.parent.mkdir(parents=True)
    problems.write_text("{not-json", encoding="utf-8")

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "problems.json" in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


def test_materialize_fails_closed_on_corrupt_counterexample(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce = change_dir / "discovery" / "counterexamples" / "CE-BAD.yaml"
    ce.parent.mkdir(parents=True)
    ce.write_text("counterexample_id: CE-BAD\n", encoding="utf-8")

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "CE-BAD.yaml" in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


def test_materialize_fails_closed_on_corrupt_candidate_or_receipt(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce = change_dir / "discovery" / "counterexamples" / "CE-001.yaml"
    ce.parent.mkdir(parents=True)
    ce.write_text(yaml.safe_dump(_confirmed_counterexample_payload("CE-001")), encoding="utf-8")
    candidate = change_dir / "discovery" / "candidates" / "RC-BAD" / "candidate.yaml"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate_id: RC-BAD\n", encoding="utf-8")

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "candidate.yaml" in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


def test_materialize_fails_closed_on_corrupt_promotion_receipt(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce = change_dir / "discovery" / "counterexamples" / "CE-001.yaml"
    ce.parent.mkdir(parents=True)
    ce.write_text(yaml.safe_dump(_confirmed_counterexample_payload("CE-001")), encoding="utf-8")
    candidate_dir = change_dir / "discovery" / "candidates" / "RC-001"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.yaml").write_text(
        yaml.safe_dump(_candidate_payload()),
        encoding="utf-8",
    )
    (candidate_dir / "promotion-receipt.json").write_text("{not-json", encoding="utf-8")

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "promotion-receipt.json" in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


@pytest.mark.parametrize(
    ("candidate_change_id", "counterexample_id", "expected"),
    (
        (CHANGE_ID, "CE-GHOST", "CE-GHOST"),
        ("CH-FOREIGN", "CE-001", "CH-FOREIGN"),
    ),
)
def test_materialize_rejects_candidate_identity_outside_current_change(
    tmp_path: Path,
    candidate_change_id: str,
    counterexample_id: str,
    expected: str,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce = change_dir / "discovery" / "counterexamples" / "CE-001.yaml"
    ce.parent.mkdir(parents=True)
    ce.write_text(yaml.safe_dump(_counterexample_payload("CE-001")), encoding="utf-8")
    candidate_dir = change_dir / "discovery" / "candidates" / "RC-001"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.yaml").write_text(
        yaml.safe_dump(
            _candidate_payload(
                change_id=candidate_change_id,
                counterexample_id=counterexample_id,
            )
        ),
        encoding="utf-8",
    )

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert expected in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


@pytest.mark.parametrize(
    ("candidate_overrides", "counterexample_overrides", "expected"),
    (
        ({"campaign_id": "CAMP-FOREIGN"}, {}, "campaign_id"),
        ({"oracle_id": "ORACLE-FOREIGN"}, {}, "oracle_id"),
        ({}, {"finding_status": "needs_review"}, "not confirmed"),
    ),
)
def test_materialize_rejects_candidate_with_mismatched_counterexample_provenance(
    tmp_path: Path,
    candidate_overrides: dict[str, object],
    counterexample_overrides: dict[str, object],
    expected: str,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce_path = change_dir / "discovery" / "counterexamples" / "CE-001.yaml"
    ce_path.parent.mkdir(parents=True)
    ce_payload = _confirmed_counterexample_payload("CE-001")
    ce_payload.update(counterexample_overrides)
    ce_path.write_text(yaml.safe_dump(ce_payload), encoding="utf-8")
    candidate_dir = change_dir / "discovery" / "candidates" / "RC-001"
    candidate_dir.mkdir(parents=True)
    candidate_payload = _candidate_payload()
    candidate_payload.update(candidate_overrides)
    (candidate_dir / "candidate.yaml").write_text(
        yaml.safe_dump(candidate_payload),
        encoding="utf-8",
    )

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert expected in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


def test_materialize_fails_closed_on_corrupt_coverage_gap_projection(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    gap_path = change_dir / COVERAGE_GAPS_REL
    gap_path.parent.mkdir(parents=True)
    gap_path.write_text("{not-json", encoding="utf-8")

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert COVERAGE_GAPS_REL in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()


def test_materialize_rejects_foreign_coverage_gap_projection(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    gap_path = change_dir / COVERAGE_GAPS_REL
    gap_path.parent.mkdir(parents=True)
    gap_path.write_bytes(
        canonical_json_bytes(
            CoverageGapsDocument(
                schema_version="1",
                change_id="CH-FOREIGN",
                batch_id="b1",
                projection_digest="sha256:" + ("f" * 64),
                gaps=(),
            )
        )
    )

    result = materialize_c_layer_metrics_operation(_task(), _workspace(tmp_path), _context(tmp_path))

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "CH-FOREIGN" in (result.error or "")
    assert not (change_dir / C_LAYER_METRICS_REL).exists()
