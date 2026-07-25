from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from assurance_agent.workflow.report.report_builder import generate_report


def _api(failed_message: str | None) -> TargetResult:
    cases = [
        CaseResult(
            case_id="TC_API_001",
            status="passed",
            file="tests/api/t.py",
            test_name="test_tc_api_001__ok",
            duration_ms=1,
            message="",
        )
    ]
    failed = 0
    if failed_message is not None:
        failed = 1
        cases.append(
            CaseResult(
                case_id="TC_API_002",
                status="failed",
                file="tests/api/t.py",
                test_name="test_tc_api_002__x",
                duration_ms=1,
                message=failed_message,
            )
        )
    return TargetResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        target="api",
        status="failed" if failed else "passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=len(cases),
        passed=len(cases) - failed,
        failed=failed,
        skipped=0,
        cases=cases,
        unmapped_tests=[],
    )


def _cov() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        available=True,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )


def _seed_change(tmp_path: Path, api: TargetResult, cov: CoverageResult) -> str:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="20260715-000000",
        api=api,
        e2e=None,
        coverage=cov,
        coverage_gate_mode="warn",
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution",
        change_id="CH-1",
        batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api,
        e2e=None,
        fuzz=None,
        coverage=cov,
        performance=None,
        quality_gate=gate,
        summary="# summary\n",
    )
    return "CH-1"


def test_generate_report_all_pass(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "PASS"
    assert result.report.quality_score == 100
    assert result.report.risk_level == "LOW"
    report_dir = tmp_path / "qa" / "changes" / "CH-1" / "report"
    assert (report_dir / "quality-report.json").is_file()
    assert (report_dir / "quality-report.md").is_file()
    assert (report_dir / "executive-summary.md").is_file()
    # No execution ledger → Start / Duration render as "No data"
    assert result.report.started_at is None and result.report.duration is None
    md = (report_dir / "quality-report.md").read_text()
    assert "**Start**: No data" in md
    assert "**Duration**: No data" in md


def test_generate_report_includes_execution_start_and_duration(tmp_path: Path) -> None:
    import json

    change_id = _seed_change(tmp_path, _api(None), _cov())
    change = tmp_path / "qa" / "changes" / "CH-1"
    # Write ledger lines directly so `ts` is under our control (append_event_strict
    # stamps ts itself and forbids overriding it via the event model).
    (change / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "seq": 1,
                        "ts": "2026-07-18T01:00:00+00:00",
                        "source": "progression",
                        "type": "dispatch_signed",
                        "phase": "execution",
                        "kind": "dispatch_phase",
                        "attempt_id": "execution:t1",
                        "state_guard": "g",
                        "dispatched_at": 0,
                    }
                ),
                json.dumps(
                    {
                        "seq": 2,
                        "ts": "2026-07-18T01:02:05+00:00",
                        "source": "progression",
                        "type": "phase_outcome_committed",
                        "phase": "execution",
                        "attempt_id": "execution:t1",
                        "gate_report": None,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.started_at == "2026-07-18T01:00:00+00:00"
    assert result.report.duration == "2m 5s"
    md = (tmp_path / "qa" / "changes" / "CH-1" / "report" / "quality-report.md").read_text()
    assert "**Start**: 2026-07-18T01:00:00+00:00" in md
    assert "**Duration**: 2m 5s" in md


def test_generate_report_business_defect_is_high_risk(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api("500 internal server error"), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "FAIL"
    assert result.report.risk_level == "HIGH"
    assert len(result.report.defects.product) == 1


def test_generate_report_known_product_issue_is_product_defect(tmp_path: Path) -> None:
    change_id = _seed_change(
        tmp_path,
        _api("expected-product-fail: known product issue documented"),
        _cov(),
    )
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.risk_level == "HIGH"
    assert len(result.report.defects.product) == 1
    assert result.report.defects.product[0].category == "known_product_issue"
    assert len(result.report.defects.test) == 0


# ---- Issue risk / schema 1.1 tests -----------------------------------------


def _seed_issue_snapshot(change_dir: Path, *, analysis_status: str = "completed", occurrences: list | None = None) -> None:
    """Write a minimal issues/snapshot.json for testing."""
    import json

    issues_dir = change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "authoritative_batch_id": "20260715-000000",
        "observations": [],
        "occurrences": occurrences or [],
        "analysis_status": {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "20260715-000000",
            "status": analysis_status,
            "evidence_bundle_digest": "abc123",
            "candidate_count": 0,
        },
        "project_sync_status": "completed",
        "batches": ["20260715-000000"],
    }
    (issues_dir / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")


def test_generate_report_emits_schema_version_11(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.schema_version == "1.1"


def test_generate_report_no_issue_snapshot_yields_none_issues(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    # No issues/snapshot.json → issues field is None
    assert result.report.issues is None
    # final_status unchanged
    assert result.report.final_status == "PASS"


def test_generate_report_no_occurrences_yields_clear_issue_risk(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _seed_issue_snapshot(change_dir, analysis_status="completed", occurrences=[])
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.issues is not None
    assert result.report.issues.issue_risk == "clear"
    # execution final_status must not be changed by Issue risk
    assert result.report.final_status == "PASS"


def test_generate_report_failed_analysis_yields_unknown_issue_risk(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _seed_issue_snapshot(change_dir, analysis_status="failed")
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.issues is not None
    assert result.report.issues.issue_risk == "unknown"
    assert result.report.issues.analysis_status == "failed"
    # execution final_status unchanged
    assert result.report.final_status == "PASS"


def test_generate_report_failed_reconcile_status_yields_unknown_without_snapshot(
    tmp_path: Path,
) -> None:
    """Semantic reconcile rejection is fail-visible even when no canonical snapshot exists."""
    import json

    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "issue-reconcile-status.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": "CH-1",
                "batch_id": "20260715-000000",
                "status": "failed",
                "evidence_bundle_digest": "sha256:" + "a" * 64,
                "error": "unknown observation_id",
            }
        ),
        encoding="utf-8",
    )
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.issues is not None
    assert result.report.issues.issue_risk == "unknown"
    assert result.report.issues.analysis_status == "failed"
    assert result.report.final_status == "PASS"


def test_generate_report_missing_analysis_status_is_not_treated_as_completed(
    tmp_path: Path,
) -> None:
    """A snapshot without analysis_status must not collapse to clear/completed."""
    import json

    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    issues_dir = change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": "CH-1",
                "authoritative_batch_id": "20260715-000000",
                "observations": [],
                "occurrences": [],
                "analysis_status": None,
                "project_sync_status": "completed",
                "batches": ["20260715-000000"],
            }
        ),
        encoding="utf-8",
    )
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.issues is not None
    assert result.report.issues.issue_risk == "unknown"
    assert result.report.issues.analysis_status == "failed"


def test_generate_report_issue_section_in_markdown(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _seed_issue_snapshot(change_dir, analysis_status="completed", occurrences=[])
    inspect_change(tmp_path, change_id)
    generate_report(tmp_path, change_id)
    md = (tmp_path / "qa" / "changes" / "CH-1" / "report" / "quality-report.md").read_text()
    assert "## Issue Risk" in md
    assert "**Issue Risk**: clear" in md


def test_generate_report_issue_risk_in_exec_summary(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _seed_issue_snapshot(change_dir, analysis_status="failed")
    inspect_change(tmp_path, change_id)
    generate_report(tmp_path, change_id)
    exec_summary = (tmp_path / "qa" / "changes" / "CH-1" / "report" / "executive-summary.md").read_text()
    assert "**Issue Risk**: unknown" in exec_summary
