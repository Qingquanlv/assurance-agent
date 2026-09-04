from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.cli_support import parse_json_output
from tests.product.test_result_export import CHANGE_ID, write_achieved

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_start_requires_explicit_product_deployment_config_and_input(cli_runner, tmp_path):
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["start", "--project-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "--project-dir" in result.output or "--change" in result.output
    assert "--change" in result.output
    assert "--invocation-id" in result.output
    assert "--product" in result.output
    assert "--binding-dist" in result.output
    assert "--binding-entrypoint" in result.output
    assert "--binding-declaration" in result.output
    assert "--config-tree" in result.output
    assert "--entrypoint" in result.output
    assert "--input" in result.output


def test_run_opens_or_starts_and_completes_with_scripted_host(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine create_engine hook was retired")


def test_repeated_run_requires_the_same_source_coordinates(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine create_engine hook was retired")


def test_resume_rejects_new_product_config_or_input(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    result = cli_runner.invoke(
        app,
        [
            "resume",
            "--project-dir",
            str(tmp_path),
            "--change",
            "CH-RESUME-001",
            "--invocation-id",
            "inv-resume-001",
            "--action",
            "approve",
            "--reason",
            "accepted",
            "--input",
            str(tmp_path / "new-input.json"),
        ],
    )
    assert result.exit_code == 2
    assert "--input" in result.output or "product" in result.output.lower() or "input" in result.output


def test_export_command_requires_project_dir(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["export", "--change", "CH-EXPORT-001"])
    assert result.exit_code == 2
    assert "--project-dir" in result.output


def test_export_uses_project_dir_and_change(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    project = write_achieved(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "export",
            "--json",
            "--project-dir",
            str(project),
            "--change",
            CHANGE_ID,
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["change_id"] == CHANGE_ID
    assert document["files"]


def test_resume_file_is_mutually_exclusive_with_action_reason(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    resume_file = tmp_path / "resume.json"
    resume_file.write_text('{"action":"approve"}\n', encoding="utf-8")
    result = cli_runner.invoke(
        app,
        [
            "resume",
            "--project-dir",
            str(tmp_path),
            "--change",
            "CH-RESUME-FILE-001",
            "--invocation-id",
            "inv-resume-file-001",
            "--product",
            "assurance-opencode",
            "--binding-dist",
            "assurance-product-bindings",
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            "plugin.yaml",
            "--config-tree",
            str(tmp_path),
            "--action",
            "approve",
            "--reason",
            "accepted",
            "--resume-file",
            str(resume_file),
        ],
    )
    assert result.exit_code == 2
    assert "resume-file" in result.output or "mutually" in result.output.lower()
