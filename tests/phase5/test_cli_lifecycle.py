from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    scripted_engine_factory,
    source_args,
)
from tests.phase5.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_start_requires_explicit_product_deployment_config_and_input(cli_runner, tmp_path):
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["start", "--engine-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "--project-dir" in result.output
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
    args, _project_dir, engine_root = common_lifecycle_args(
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
    assert len(document["seed_tree_id"]) == 64
    assert len(document["root_input_digest"]) == 64
    invocation = engine_root / "invocations" / "inv-start-001"
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
    args, _project_dir, _engine_root = common_lifecycle_args(
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
    args, _project_dir, _engine_root = common_lifecycle_args(
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
            "--engine-root",
            str(tmp_path),
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


def test_export_command_requires_destination_and_source_coordinates(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["export", "--engine-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "--destination" in result.output or "--invocation-id" in result.output


def test_export_materializes_completed_head(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-export-001",
    )
    run = cli_runner.invoke(app, ["run", *args])
    assert run.exit_code == 0, run.output
    destination = tmp_path / "export-tree"
    result = cli_runner.invoke(
        app,
        [
            "export",
            "--json",
            "--destination",
            str(destination),
            "--engine-root",
            args[args.index("--engine-root") + 1],
            "--invocation-id",
            "inv-export-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert destination.is_dir()
    assert len(document["tree_id"]) == 64
    inplace = cli_runner.invoke(
        app,
        [
            "export",
            "--json",
            "--destination",
            str(project_dir),
            "--engine-root",
            args[args.index("--engine-root") + 1],
            "--invocation-id",
            "inv-export-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert inplace.exit_code == 40, inplace.output
