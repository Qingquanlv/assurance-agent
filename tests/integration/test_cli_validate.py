import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

from assurance_agent.cli import main

VALID_REVIEW = '{"schema_version": "1.0", "decision": "pass", "findings": []}'
INVALID_REVIEW = '{"schema_version": "1.0", "decision": "maybe", "findings": []}'


def make_change(change_id: str = "CH-1") -> Path:
    write_aa_config(Path.cwd())
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    return change_dir


def test_validate_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["validate", "--change", "NOPE"])
        assert result.exit_code == 1
        assert "not found" in result.output


def test_validate_rejects_unsafe_change_id() -> None:
    result = CliRunner().invoke(main, ["validate", "--change", "../outside"])
    assert result.exit_code == 1
    assert "unsafe change id" in result.output


def test_validate_all_pass_exits_0_with_human_output() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        assert "review/case-review.json [review]" in result.output


def test_validate_failure_exits_1_and_lists_errors() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(INVALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "decision" in result.output


def test_validate_json_outputs_ok_and_results() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["ok"] is True
        assert doc["results"][0]["artifact_type"] == "review"
        assert doc["results"][0]["errors"] == []


def test_validate_unknown_phase_exits_2() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--phase", "bogus"])
        assert result.exit_code == 2
        assert "unknown phase" in result.output


def test_validate_phase_filters_to_produced_artifacts() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / ".qa.yaml").write_text(
            """schema_version: "1.0"
schema: qa-yaml/v1
created_at: "2026-07-15T00:00:00Z"
change:
  change_id: CH-1
  requirement_id: REQ-1
  feature_name: menus
  status: in_progress
targets:
  cases:
    - module: menus
      change_case_file: cases/menus/case.yaml
      target_case_file: qa/cases/menus/case.yaml
""",
            encoding="utf-8",
        )
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        # packaged v2: case-design outputs .qa.yaml, proposal.md, cases/, and the MRC matrix.
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--phase", "case-design", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert [r["path"] for r in doc["results"]] == [".qa.yaml"]


def test_validate_single_artifact_missing_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["validate", "--change", "CH-1", "--artifact", "review/case-review.json"]
        )
        assert result.exit_code == 1
        assert "file not found" in result.output


def test_validate_no_registered_artifact_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "proposal.md").write_text("# free-form", encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "no registered artifacts found" in result.output
