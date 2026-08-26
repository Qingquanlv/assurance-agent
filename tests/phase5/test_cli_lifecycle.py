from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    scripted_engine_factory,
)
from tests.phase5.composition_harness import request_for
from tests.phase5.test_result_export import CHANGE_ID, write_achieved

pytestmark = pytest.mark.usefixtures("installed_sources")


def _change_runtime(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id / ".runtime"


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


def test_start_creates_invocation_without_driving(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-start-001",
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["invocation_id"] == "inv-start-001"
    assert document["lock_digest"] == composition.lock_digest
    assert document["composition_digest"] == composition.digest
    assert "seed_tree_id" not in document
    assert len(document["root_input_digest"]) == 64
    invocation = _change_runtime(project_dir, change_id) / "invocations" / "inv-start-001"
    assert invocation.is_dir()
    ledger_events = list((invocation / "ledger").rglob("*"))
    assert ledger_events


def test_run_opens_or_starts_and_completes_with_scripted_host(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-run-001",
    )
    result = cli_runner.invoke(app, ["run", *args])
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["status"] in {"completed", "succeeded"}
    assert document["invocation_id"] == "inv-run-001"


def test_repeated_run_requires_the_same_source_coordinates(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-repeat-001",
    )
    first = cli_runner.invoke(app, ["run", *args])
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(app, ["run", *args])
    assert second.exit_code == 0, second.output
    drifted = list(args)
    product_index = drifted.index("--product") + 1
    drifted[product_index] = "assurance-cursor"
    third = cli_runner.invoke(app, ["run", *drifted])
    assert third.exit_code == 40, third.output


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
