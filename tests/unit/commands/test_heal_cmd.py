import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _change(root: Path) -> Path:
    change = root / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    return change


def test_validate_proposal_ok(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(
        change / "healing" / "fix-proposal.json",
        {
            "schema_version": "1.0",
            "summary": {"eligible_count": 1},
            "proposals": [{"target": "e2e", "eligible": True}],
        },
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "validate-proposal", "--change", "CH-1"])
    assert result.exit_code == 0
    assert "eligible_count" in result.output


def test_validate_proposal_missing_exit_40(tmp_path: Path, monkeypatch) -> None:
    _change(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "validate-proposal", "--change", "CH-1"])
    assert result.exit_code == 40


def test_safety_check_passed_exit_0(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=True, needs_review=False))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 0


def test_safety_check_needs_review_exit_30(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=True, needs_review=True))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 30


def test_safety_check_failed_exit_40(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=False, needs_review=False))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 40


def _safety(*, passed: bool, needs_review: bool) -> dict:
    return {
        "schema_version": "1.0",
        "passed": passed,
        "needs_review": needs_review,
        "product_code_modified": False,
        "skip_or_xfail_added": False,
        "unrelated_tests_modified": False,
        "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
    }
