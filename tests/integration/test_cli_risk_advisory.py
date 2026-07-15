import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

CONTEXT = {
    "schema_version": "1.0",
    "change_id": "CH-1",
    "generated_at": "2026-07-15T00:00:00+00:00",
    "aggregation_policy": {},
    "archive_window": {},
    "staleness": {"max_age_days": 30, "stale": False},
    "impact": {
        "diff_base": "main",
        "changed_files": [],
        "modules": [],
        "affected_case_ids": ["TC_MENU_001"],
        "affected_cases_by_module": {},
        "affected_test_files": [],
    },
    "case_signals": [],
    "test_health": [],
    "historical_issues": [],
    "evidence": [
        {
            "id": "EV-DIFF-MENUS-HIGH",
            "type": "code_change",
            "module": "menus",
            "confidence": "high",
            "source": "git diff",
        }
    ],
    "degraded": False,
    "degraded_reasons": [],
}


def seed_explore(change_id: str, advisory: dict) -> Path:
    explore = Path("qa/changes") / change_id / "explore"
    explore.mkdir(parents=True)
    (explore / "context.json").write_text(json.dumps(CONTEXT), encoding="utf-8")
    (explore / "advisory.json").write_text(json.dumps(advisory), encoding="utf-8")
    return explore


def test_validate_advisory_missing_context_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("qa/changes/CH-1/explore").mkdir(parents=True)
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "Missing" in result.output


def test_validate_advisory_pass_exits_0() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
                "open_questions_for_case_design": [],
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        assert "passed" in result.output


def test_validate_advisory_semantic_failure_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-NOPE"]}],
                "open_questions_for_case_design": [],
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "unknown id" in result.output
