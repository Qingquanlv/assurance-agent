import base64
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


def test_write_advisory_materializes_valid_matching_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        payload = {"schema_version": "1.0", "change_id": "CH-1", "watchlist": []}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()

        result = runner.invoke(
            main,
            ["risk", "write-advisory", "--change", "CH-1", "--payload-base64", encoded],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(Path("qa/changes/CH-1/explore/advisory.json").read_text()) == payload


def test_write_advisory_rejects_mismatched_change_id() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        encoded = base64.b64encode(json.dumps({"change_id": "OTHER"}).encode()).decode()

        result = runner.invoke(
            main,
            ["risk", "write-advisory", "--change", "CH-1", "--payload-base64", encoded],
        )

        assert result.exit_code == 1
        assert "must match" in result.output
        assert not Path("qa/changes/CH-1/explore/advisory.json").exists()


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


def test_validate_advisory_accepts_case_id_from_qa_cases() -> None:
    """case_id 存在于 qa/cases（但不在 context.affected_case_ids）时不应误拒。"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        case_dir = Path("qa/cases/menus")
        case_dir.mkdir(parents=True)
        (case_dir / "case.yaml").write_text(
            "cases:\n  - case_id: TC_LEGAL_001\n    module: menus\n", encoding="utf-8"
        )
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
                "open_questions_for_case_design": [],
                "case_design_guidance": {
                    "priority_hints": [
                        {
                            "case_id": "TC_LEGAL_001",
                            "confidence": "high",
                            "evidence_ids": ["EV-DIFF-MENUS-HIGH"],
                        }
                    ]
                },
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        assert "passed" in result.output


def test_validate_advisory_rejects_unknown_case_id() -> None:
    """qa/cases 与 affected_case_ids 都没有的 case_id 仍应被拒。"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
                "open_questions_for_case_design": [],
                "case_design_guidance": {
                    "priority_hints": [
                        {
                            "case_id": "TC_NOPE_999",
                            "confidence": "high",
                            "evidence_ids": ["EV-DIFF-MENUS-HIGH"],
                        }
                    ]
                },
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "not in context.affected_case_ids or qa/cases" in result.output
