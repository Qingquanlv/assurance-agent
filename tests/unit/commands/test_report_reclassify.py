import json
from pathlib import Path

from tests.helpers_aa import write_aa_config
from unittest.mock import patch

from click.testing import CliRunner

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.cli import main
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def _publish_failed_batch(root: Path, batch_id: str = "20260715-000001") -> None:
    write_aa_config(root)
    (root / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)
    cases = [
        CaseResult(
            case_id="TC_API_001",
            status="failed",
            file="f.py",
            test_name="test_tc_api_001__x",
            duration_ms=1,
            message="Connection refused: server unreachable",
        ),
    ]
    api = TargetResult(
        change_id="CH-1",
        batch_id=batch_id,
        target="api",
        status="failed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=1,
        passed=0,
        failed=1,
        skipped=0,
        cases=cases,
        unmapped_tests=[],
    )
    cov = CoverageResult(
        change_id="CH-1",
        batch_id=batch_id,
        available=True,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )
    gate = build_quality_gate(
        change_id="CH-1", batch_id=batch_id, api=api, e2e=None, coverage=cov, coverage_gate_mode="warn"
    )
    publish_execution_evidence(
        execution_dir=root / "qa" / "changes" / "CH-1" / "execution",
        change_id="CH-1",
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


def test_reclassify_updates_category_and_writes_telemetry(tmp_path: Path, monkeypatch) -> None:
    _publish_failed_batch(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    first = runner.invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert first.exit_code == 1
    analysis_path = tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "failure-analysis.json"
    before = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert before["failures"][0]["category"] == "environment_failure"

    events: list[dict] = []

    def capture_event(change_dir, event):  # noqa: ANN001
        events.append(event)

    monkeypatch.setattr(
        "assurance_agent.commands.report_cmd.append_event_best_effort",
        capture_event,
    )

    with patch(
        "assurance_agent.workflow.report.inspector.classify_failure",
        return_value=type(
            "C",
            (),
            {
                "category": "assertion_failure",
                "fix_proposal_eligible": False,
                "severity": "medium",
                "needs_review": False,
            },
        )(),
    ):
        result = runner.invoke(
            main,
            [
                "report",
                "reclassify",
                "--change",
                "CH-1",
                "--batch",
                "20260715-000001",
            ],
        )

    assert result.exit_code == 1
    after = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert after["source_batch_id"] == "20260715-000001"
    assert after["failures"][0]["category"] == "assertion_failure"
    assert after["failures"][0]["reclassified"]["from"] == "environment_failure"
    assert events and events[-1]["type"] == "reclassified"


def test_reclassify_unsafe_batch_id_exit_one(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "report",
            "reclassify",
            "--change",
            "CH-1",
            "--batch",
            "../x",
        ],
    )
    assert result.exit_code == 1
