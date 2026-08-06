"""Verification Metrics M1 Task 9 — benchmark/eval fixture acceptance.

Uses existing benchmark archive + eval-fixtures samples. Does **not** rewrite
historical archive artifacts. Covers A1–A4 / B2 / B5 collectors, P0 risk lower
bound, MRC shadow parity, metrics human-interrupt routing, and report combine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.artifacts.models.cases import CaseEntry, CaseRisk
from assurance_agent.artifacts.models.inspect import PerformanceScenarioVerdict
from assurance_agent.artifacts.models.metrics import MetricsDocument
from assurance_agent.artifacts.models.minimum_coverage import (
    MinimumCoverageMatrix,
    maps_from_advisory_mrc,
)
from assurance_agent.artifacts.models.trace import TraceExecution, TraceProjection, TraceRow
from assurance_agent.evidence.risk_tier import resolve_risk_tier
from assurance_agent.verification.property_scan import PropertyMarkerHit
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PropertyTestResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.metrics.auth_matrix import compute_auth_matrix
from assurance_agent.workflow.metrics.constraint_coverage import compute_constraint_coverage
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage
from assurance_agent.workflow.metrics.journey_coverage import compute_journey_coverage
from assurance_agent.workflow.metrics.minimum_coverage import (
    materialize_minimum_coverage,
    obligations_from_matrix,
    shadow_compare_minimum_coverage,
)
from assurance_agent.workflow.metrics.threshold_slack import compute_threshold_slack
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from assurance_agent.workflow.report.report_builder import generate_report
from tests.helpers_aa import write_aa_config

REPO = Path(__file__).resolve().parents[3]
BENCHMARK = REPO / "benchmark" / "vue-fastapi-admin"
ARCHIVE = BENCHMARK / "qa" / "archive" / "RET-dept-management-20260725-124844-cursor"
EVAL_SAMPLE = BENCHMARK / "eval-fixtures" / "samples" / "eval-sample-001"
TS = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
CONSTRAINT_KEY = "entities.dept.constraints.name_unique"
STRONG_SOURCE = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_duplicate_name_rejected():
    response = {"code": 400, "detail": "name exists"}
    assert response["code"] == 400
    assert response["detail"] == "name exists"
"""


pytestmark = pytest.mark.skipif(
    not ARCHIVE.is_dir() or not EVAL_SAMPLE.is_dir(),
    reason="benchmark archive / eval-sample-001 fixtures missing",
)


def test_a1_diff_coverage_on_fixture_shaped_inputs() -> None:
    """A1: diff line coverage from coverage.json × changed-lines (archive-shaped)."""
    evidence = collect_diff_coverage(
        change_id="RET-dept-management",
        batch_id="20260725-132205",
        coverage_json={
            "files": {"app/models/admin.py": {"executed_lines": [10, 11, 12, 20]}},
        },
        changed_lines={"app/models/admin.py": [10, 11, 99]},
    )
    assert evidence.value == pytest.approx(2 / 3)
    assert evidence.collection_gaps == ()


def test_a2_and_b2_constraint_coverage_requires_strong_oracle() -> None:
    """A2 + B2: marker + passed + fresh + strong assertion oracle."""
    evidence = compute_constraint_coverage(
        change_id="RET-dept-management",
        batch_id="20260725-132205",
        known_keys=frozenset({CONSTRAINT_KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            PropertyTestResult(
                nodeid="tests/api/test_props.py::test_duplicate_name_rejected",
                file="tests/api/test_props.py",
                constraint_keys=(CONSTRAINT_KEY,),
                outcome="passed",
                batch_id="20260725-132205",
            ),
        ),
        marker_hits=(
            PropertyMarkerHit(
                file="tests/api/test_props.py",
                test_name="test_duplicate_name_rejected",
                constraint_keys=(CONSTRAINT_KEY,),
                lineno=4,
            ),
        ),
        test_sources={"tests/api/test_props.py": STRONG_SOURCE},
    )
    assert evidence.declared is not None
    assert evidence.declared.covered == 1
    assert evidence.value == 1.0


def test_a3_auth_matrix_empty_matrix_is_collection_gap() -> None:
    """A3: empty declared matrix fail-closes (benchmark L1 often has no cells yet)."""
    evidence = compute_auth_matrix(
        change_id="eval-sample-001",
        batch_id="20260716-194559",
        cells={},
        executions=(),
    )
    assert evidence.value is None
    assert evidence.collection_gaps
    assert evidence.collection_gaps[0].metric == "auth_matrix_coverage"


def test_a4_journey_coverage_honors_archive_mrc_keys() -> None:
    """A4: journey join against archive MRC matrix + empty projection."""
    matrix = MinimumCoverageMatrix.model_validate(
        yaml.safe_load((ARCHIVE / "trace/minimum-coverage-matrix.yaml").read_text(encoding="utf-8"))
    )
    advisory = json.loads((ARCHIVE / "explore/advisory.json").read_text(encoding="utf-8"))
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(
        advisory.get("minimum_required_coverage")
    )
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    journeys = tuple(o for o in lifted.obligations if o.category in {"e2e", "e2e_if_enabled"})
    assert journeys, "archive advisory should declare journey MRC items"
    evidence = compute_journey_coverage(
        change_id="RET-dept-management",
        batch_id="20260725-132205",
        obligations=journeys,
        projection=TraceProjection(
            schema_version="1",
            change_id="RET-dept-management",
            phase="reconciled",
            authoritative_batch_id="20260725-132205",
            sources=(),
            rows=(),
            unmapped_tests=(),
            gaps=(),
            integrity="complete",
        ),
        quarantine=(),
        test_sources={},
        case_functions={},
    )
    assert evidence.declared is not None
    assert evidence.declared.covered == 0


def test_b5_threshold_slack_from_perf_scenario_shape() -> None:
    """B5: threshold/measured slack; out-of-band only shortboards."""
    evidence = compute_threshold_slack(
        change_id="eval-sample-001",
        batch_id="20260716-194559",
        scenarios=(
            PerformanceScenarioVerdict(
                capability="api_list",
                endpoint="/api/v1/dept/list",
                measured_p95_ms=500.0,
                threshold_p95_ms=2000.0,
                measured_error_rate=0.0,
                threshold_error_rate_max=0.01,
                verdict="PASS",
            ),
        ),
    )
    assert evidence.value == 4.0
    assert evidence.shortboards == ()


def test_p0_risk_lower_bound_from_eval_sample_cases() -> None:
    """P0 cases in eval-sample-001 mechanically lower-bound at high."""
    cases_path = EVAL_SAMPLE / "cases" / "system" / "api" / "case.yaml"
    raw = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    entries: list[CaseEntry] = []
    for bucket in ("added", "modified"):
        for item in raw.get(bucket) or []:
            if item.get("priority") == "P0":
                entries.append(CaseEntry.model_validate(item))
    assert entries, "eval-sample-001 should include P0 API cases"
    # Author a medium declaration (observed LLM downgrade) — bound still high.
    first = entries[0]
    downgraded = first.model_copy(
        update={
            "risk": CaseRisk(
                likelihood=3,
                impact=3,
                level="medium",
                rationale="fixture acceptance",
            )
        }
    )
    resolution = resolve_risk_tier([downgraded])
    assert resolution.lower_bound == "high"
    assert resolution.tier == "high"
    assert resolution.declaration_lowered is True


def test_mrc_shadow_matches_archived_skill_result() -> None:
    """MRC shadow: deterministic join ≡ archived skill JSON on same inputs."""
    legacy = json.loads((ARCHIVE / "report/minimum-coverage-result.json").read_text(encoding="utf-8"))
    matrix = MinimumCoverageMatrix.model_validate(
        yaml.safe_load((ARCHIVE / "trace/minimum-coverage-matrix.yaml").read_text(encoding="utf-8"))
    )
    advisory = json.loads((ARCHIVE / "explore/advisory.json").read_text(encoding="utf-8"))
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(
        advisory.get("minimum_required_coverage")
    )
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    batch_dirs = sorted((ARCHIVE / "execution" / "runs").iterdir())
    batch_id = batch_dirs[-1].name
    rows: list[TraceRow] = []
    for target, filename, case_type in (
        ("api", "api-result.json", "API"),
        ("e2e", "e2e-result.json", "E2E"),
    ):
        payload = json.loads((batch_dirs[-1] / filename).read_text(encoding="utf-8"))
        for case in payload.get("cases") or []:
            status = case["status"]
            latest = TraceExecution(
                batch_id=batch_id,
                target=target,  # type: ignore[arg-type]
                status=status,
                ts=TS,
                ts_source="executed_at",
            )
            rows.append(
                TraceRow(
                    case_id=case["case_id"],
                    module="system.dept",
                    case_type=case_type,  # type: ignore[arg-type]
                    automation_required=True,
                    assertions=("archive fixture",),
                    covering_tests=(),
                    coverage_state="covered",
                    latest_execution=latest,
                    freshest_pass=latest if status == "passed" else None,
                    presence_in_current_batch="executed",
                    atemporal_kinds_present=(),
                    open_problem_ids=(),
                )
            )
    projection = TraceProjection(
        schema_version="1",
        change_id=legacy["change_id"],
        phase="reconciled",
        authoritative_batch_id=batch_id,
        sources=(),
        rows=tuple(rows),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )
    result = materialize_minimum_coverage(
        change_id=legacy["change_id"],
        obligations=lifted.obligations,
        projection=projection,
    )
    assert shadow_compare_minimum_coverage(result, legacy) == []


def test_metrics_human_interrupt_route_on_collection_gaps(tmp_path: Path) -> None:
    """Collection gaps → metrics-sufficiency-gate reject (blocks archive, keeps flowing)."""
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True)
    doc = MetricsDocument.model_validate(
        {
            "schema_version": "2",
            "change_id": "CH-1",
            "cadence": "pr",
            "computed_at": "2026-08-05T02:00:00+00:00",
            "risk_tier": "low",
            "risk_tier_lower_bound": "low",
            "risk_tier_declared": None,
            "risk_declaration_lowered": False,
            "risk_lowered_declarations": [],
            "metrics": {
                "constraint_coverage": {
                    "layer": "api",
                    "status": "collection_failed",
                    "value": None,
                    "declared": None,
                    "evidence": "",
                }
            },
            "collection_gaps": [
                {
                    "code": "collection_failed",
                    "detail": "fixture",
                    "metric": "constraint_coverage",
                }
            ],
            "shortboards": [],
            "floor_ratio": None,
            "policy_digest": "0" * 64,
        }
    )
    (change_dir / "inspect" / "metrics.json").write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    schema = load_workflow_v2(Path.cwd())
    report = check_gate_in_view(
        schema.gates,
        "metrics-sufficiency-gate",
        GateEvaluationContext(
            project_root=tmp_path,
            repo_root=tmp_path,
            change_dir=change_dir,
            change_id="CH-1",
            params={},
            state_values={},
            node_results={},
        ),
    )
    assert report.verdict.value == "reject"
    # Interrupt node still exists for require_human numeric floor misses.
    assert "metrics-sufficiency-review" in schema.graphs["assurance"].nodes


def test_report_combines_metrics_without_rewriting_gate(tmp_path: Path) -> None:
    """Report: combine quality gate + inspect/metrics.json; gate bytes unchanged."""
    write_aa_config(tmp_path)
    change_id = "CH-1"
    change_dir = tmp_path / "qa" / "changes" / change_id
    change_dir.mkdir(parents=True)
    batch_id = "20260716-194559"
    api = TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target="api",
        status="passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=1,
        passed=1,
        failed=0,
        skipped=0,
        cases=[
            CaseResult(
                case_id="TC_API_001",
                status="passed",
                file="t.py",
                test_name="test_tc_api_001__ok",
                duration_ms=1,
                message="",
            )
        ],
        unmapped_tests=[],
    )
    cov = CoverageResult(
        change_id=change_id,
        batch_id=batch_id,
        available=False,
        line_coverage=0.0,
        branch_coverage=0.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="SKIPPED",
        skip_reason="fixture",
    )
    gate = build_quality_gate(
        change_id=change_id,
        batch_id=batch_id,
        api=api,
        e2e=None,
        coverage=cov,
        coverage_gate_mode="warn",
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution",
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api,
        e2e=None,
        fuzz=None,
        coverage=cov,
        performance=None,
        quality_gate=gate,
        summary="# summary\n",
    )
    inspect_change(tmp_path, change_id)
    gate_path = change_dir / "inspect" / "quality-gate-result.json"
    before = gate_path.read_bytes()
    metrics = {
        "schema_version": "2",
        "change_id": change_id,
        "cadence": "pr",
        "computed_at": "2026-08-05T02:00:00+00:00",
        "risk_tier": "high",
        "risk_tier_lower_bound": "high",
        "risk_tier_declared": "medium",
        "risk_declaration_lowered": True,
        "risk_lowered_declarations": [],
        "metrics": {
            "constraint_coverage": {
                "layer": "api",
                "status": "evaluated",
                "value": 1.0,
                "declared": {"total": 1, "covered": 1, "value": 1.0, "uncovered": []},
                "touched": None,
                "holds": None,
                "surfaces": [],
                "evidence": "constraint-coverage.json",
            }
        },
        "collection_gaps": [],
        "shortboards": [],
        "floor_ratio": None,
        "policy_digest": "0" * 64,
    }
    (change_dir / "inspect" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    result = generate_report(tmp_path, change_id)
    assert gate_path.read_bytes() == before
    assert result.report.metrics is not None
    assert result.report.metrics["risk_tier"] == "high"
    # Attribution: eval-sample golden quality-gate has no metrics dimension;
    # new report attaches metrics without rewriting that gate artifact.
    golden_gate = json.loads(
        (EVAL_SAMPLE / "execution" / "quality-gate-result.json").read_text(encoding="utf-8")
    )
    assert "metrics" not in (golden_gate.get("dimensions") or {})
