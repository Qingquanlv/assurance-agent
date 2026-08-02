import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main
from tests.helpers_aa import write_aa_config
from tests.unit.artifacts.test_validate import (
    VALID_QUALITY_V1,
    VALID_QUALITY_V2,
    VALID_RECONCILE_V1,
    VALID_RECONCILE_V2,
    VALID_TRACE_V1,
    VALID_TRACE_V2,
)

VALID_REVIEW = '{"schema_version": "1.0", "decision": "pass", "findings": []}'
INVALID_REVIEW = '{"schema_version": "1.0", "decision": "maybe", "findings": []}'


def make_change(change_id: str = "CH-1") -> Path:
    write_aa_config(Path.cwd())
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    return change_dir


def _write_inspect(change_dir: Path, rel: str, payload: dict[str, object]) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
        # packaged v2: case-design outputs [.qa.yaml, proposal.md, cases/]
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


@pytest.mark.parametrize(
    ("relpath", "payload", "artifact_label"),
    [
        ("inspect/trace-projection.json", VALID_TRACE_V1, "trace_projection"),
        ("inspect/trace-projection.json", VALID_TRACE_V2, "trace_projection"),
        (
            "inspect/quality-gate-result.json",
            {**VALID_QUALITY_V1, "change_id": "CH-1"},
            "quality_gate_result",
        ),
        (
            "inspect/quality-gate-result.json",
            {**VALID_QUALITY_V2, "change_id": "CH-1"},
            "quality_gate_result",
        ),
        ("inspect/issue-reconcile-status.json", VALID_RECONCILE_V1, "issue_reconcile_status"),
        ("inspect/issue-reconcile-status.json", VALID_RECONCILE_V2, "issue_reconcile_status"),
    ],
)
def test_validate_cli_accepts_wire_compat_versions(
    relpath: str,
    payload: dict[str, object],
    artifact_label: str,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        _write_inspect(change_dir, relpath, payload)
        result = runner.invoke(
            main,
            ["validate", "--change", "CH-1", "--artifact", relpath, "--json"],
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["ok"] is True
        assert doc["results"][0]["artifact_type"] == artifact_label


@pytest.mark.parametrize(
    ("relpath", "payload"),
    [
        ("inspect/trace-projection.json", {**VALID_TRACE_V1, "schema_version": None}),
        ("inspect/trace-projection.json", {**VALID_TRACE_V1, "schema_version": "99"}),
        (
            "inspect/quality-gate-result.json",
            {**VALID_QUALITY_V1, "schema_version": None, "change_id": "CH-1"},
        ),
        (
            "inspect/quality-gate-result.json",
            {**VALID_QUALITY_V1, "schema_version": "99", "change_id": "CH-1"},
        ),
        ("inspect/issue-reconcile-status.json", {**VALID_RECONCILE_V2, "schema_version": None}),
        ("inspect/issue-reconcile-status.json", {**VALID_RECONCILE_V2, "schema_version": "99"}),
    ],
)
def test_validate_cli_rejects_null_or_unknown_schema_version(
    relpath: str,
    payload: dict[str, object],
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        _write_inspect(change_dir, relpath, payload)
        result = runner.invoke(
            main,
            ["validate", "--change", "CH-1", "--artifact", relpath, "--json"],
        )
        assert result.exit_code == 1, result.output
        doc = json.loads(result.output)
        assert doc["ok"] is False
        assert doc["results"][0]["errors"]


def test_validate_cli_accepts_mixed_legacy_and_current_wire_tree() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change("CH-MIX")
        _write_inspect(change_dir, "inspect/trace-projection.json", VALID_TRACE_V1)  # missing version
        _write_inspect(
            change_dir,
            "inspect/quality-gate-result.json",
            {**VALID_QUALITY_V2, "change_id": "CH-MIX"},
        )
        _write_inspect(
            change_dir,
            "inspect/issue-reconcile-status.json",
            {**VALID_RECONCILE_V1, "change_id": "CH-MIX"},
        )
        result = runner.invoke(main, ["validate", "--change", "CH-MIX", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["ok"] is True
        types = {r["artifact_type"] for r in doc["results"]}
        assert types == {
            "trace_projection",
            "quality_gate_result",
            "issue_reconcile_status",
        }
