from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

_ANALYSIS_FILES = (
    "retro-eval-analysis.json",
    "retro-issue-analysis.json",
    "retro-workflow-analysis.json",
)


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _write_analysis_files(retro_dir: Path) -> None:
    for domain, filename in zip(("eval", "issue", "workflow"), _ANALYSIS_FILES, strict=True):
        payload = {
            "schema_version": "3",
            "retro_id": "retro-1",
            "domain": domain,
            "analysis_status": "ok",
            "failure_reason": None,
            "signals": [],
            "candidates": [],
        }
        (retro_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _write_context(retro_dir: Path, *, change_ids: tuple[str, ...] = ("CH-1",)) -> None:
    payload = {
        "schema_version": "3",
        "retro_id": "retro-1",
        "generated_at": "2026-09-16T12:00:00+00:00",
        "dry_run": False,
        "window": {
            "selection": {"mode": "change_ids", "requested_change_ids": list(change_ids)},
            "change_ids": list(change_ids),
        },
        "source_manifest": {
            "issue_slice_sha256": "sha256:" + "a" * 64,
            "workflow_slice_sha256": "sha256:" + "b" * 64,
            "eval_slice_sha256": "sha256:" + "c" * 64,
        },
        "integrity": {"status": "complete", "reasons": []},
        "domain_status": {
            "issue": {"status": "ok"},
            "workflow": {"status": "ok"},
            "eval": {"status": "ok"},
        },
        "signals": {},
        "signal_count": 0,
    }
    (retro_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")


def test_help_exposes_retro_command_tree(cli_runner: CliRunner) -> None:
    from assurance_product.cli import app
    from tests.product.cli_support import command_names, nested_command_names

    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "retro" in command_names(result.stdout)
    assert nested_command_names(cli_runner, app, "retro") == {"show"}


def test_retro_show_on_full_artifacts_exits_zero(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project_dir = tmp_path / "project"
    retro_dir = project_dir / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    _write_context(retro_dir)

    result = cli_runner.invoke(app, ["retro", "show", "--project-dir", str(project_dir), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == "1"
    assert document["stages"] == {"analyses": True, "synthesis": True, "reconcile": False}
    assert document["change_id"] == "CH-1"
    assert result.stdout.strip() == json.dumps(document, sort_keys=True, separators=(",", ":"))


def test_retro_show_missing_project_dir_exits_2(cli_runner: CliRunner) -> None:
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["retro", "show", "--json"])
    assert result.exit_code == 2


def test_retro_show_change_outside_window_exits_40(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project_dir = tmp_path / "project"
    retro_dir = project_dir / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    _write_context(retro_dir, change_ids=("CH-1",))

    result = cli_runner.invoke(
        app, ["retro", "show", "--project-dir", str(project_dir), "--change", "CH-OTHER", "--json"]
    )
    assert result.exit_code == 40, result.output


def test_retro_show_no_artifacts_exits_zero_with_all_stages_false(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    from assurance_product.cli import app

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = cli_runner.invoke(app, ["retro", "show", "--project-dir", str(project_dir), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["stages"] == {"analyses": False, "synthesis": False, "reconcile": False}


def test_retro_show_partial_artifacts_reflect_actual_stages(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project_dir = tmp_path / "project"
    retro_dir = project_dir / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)

    result = cli_runner.invoke(app, ["retro", "show", "--project-dir", str(project_dir), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["stages"] == {"analyses": True, "synthesis": False, "reconcile": False}


def test_retro_show_relative_project_dir_exits_40(cli_runner: CliRunner) -> None:
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["retro", "show", "--project-dir", "relative/path", "--json"])
    assert result.exit_code == 40, result.output
