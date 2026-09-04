from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    source_args,
    write_product_input,
    write_project_dir,
)

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_unknown_entrypoint_fails_closed(
    cli_runner, installed_sources, opencode_composition, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = opencode_composition
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-unknown-001",
        entrypoint="not-an-entrypoint",
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_missing_secret_handle_fails_closed(
    cli_runner, installed_sources, opencode_composition, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = opencode_composition
    project_dir = write_project_dir(tmp_path / "project")
    input_path = write_product_input(tmp_path / "input.json", composition)
    result = cli_runner.invoke(
        app,
        [
            "start",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-DEMO-001",
            "--invocation-id",
            "inv-secret-001",
            *source_args(installed_sources),
            "--entrypoint",
            "intake",
            "--input",
            str(input_path),
        ],
    )
    assert result.exit_code == 40, result.output


def test_missing_invocation_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = write_project_dir(tmp_path / "project")
    ChangeWorkspace.prepare(project_dir, "CH-MISSING-001")
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-MISSING-001",
            "--invocation-id",
            "inv-missing-001",
            *source_args(installed_sources),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output


def test_wrong_authorization_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine status authorization was retired")


def test_invalid_resume_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine create_engine hook was retired")


def test_active_run_conflict_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine claim holder was retired")


def test_modular_runner_rejects_legacy_lock_without_mutating_ledger(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine lock replay was retired")


def test_binding_entrypoint_must_be_deployment(cli_runner, installed_sources):
    from assurance_product.cli import app

    args = source_args(installed_sources)
    args[args.index("--binding-entrypoint") + 1] = "wildcard"
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code in {2, 40}, result.output


def test_cli_rejects_graph_import_and_workflow_overrides(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    for extra in (
        ["--graph", str(tmp_path / "graph.py")],
        ["--workflow", str(tmp_path / "workflow.yaml")],
        ["--execution-contracts", str(tmp_path / "execution-contracts.yaml")],
        ["--python-import", "sut.graphs:build"],
    ):
        result = cli_runner.invoke(app, ["compile", "--json", *extra])
        assert result.exit_code == 2, extra
        assert "no such option" in result.output.lower() or "no such option" in str(result.exception).lower()


def test_resume_file_rejects_unknown_and_duplicate_ids(cli_runner, tmp_path: Path):
    from assurance_product.application import parse_resume_file

    missing = tmp_path / "missing.json"
    missing.write_text('{"interrupt_id":"unknown","action":"approve"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown|missing|duplicate"):
        parse_resume_file(missing, pending_ids=("known",))
    duplicates = tmp_path / "dup.json"
    duplicates.write_text(
        '{"interrupts":{"a":{"action":"approve"},"a":{"action":"reject"}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate|unknown|missing"):
        parse_resume_file(duplicates, pending_ids=("a", "b"))
    scalar = tmp_path / "scalar.json"
    scalar.write_text('"approve"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_resume_file(scalar, pending_ids=("a", "b"))
