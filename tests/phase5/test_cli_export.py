from __future__ import annotations

import json
from importlib.resources import files
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


def _export_schema() -> dict[str, object]:
    raw = files("assurance_product").joinpath("resources/schemas/result-export-v1.json").read_bytes()
    return json.loads(raw.decode("utf-8"))


def test_cli_export_writes_authenticated_result_export(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.models import ResultExportV1
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-cli-export-001",
    )
    run = cli_runner.invoke(app, ["run", *args])
    assert run.exit_code == 0, run.output
    destination = tmp_path / "cli-export"
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
            "inv-cli-export-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    exported = ResultExportV1.model_validate({key: document[key] for key in ResultExportV1.model_fields})
    assert exported.schema_version == "1"
    assert exported.status.status == "completed"
    assert exported.lock_digest == composition.lock_digest
    assert (destination / "manifest.json").is_file()
    assert (destination / "result-tree").is_dir()
    schema = _export_schema()
    assert schema["title"] == "ResultExportV1"
    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    for key in required:
        assert key in document


def test_cli_export_refuses_running_invocation(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-cli-export-running",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    destination = tmp_path / "running-cli-export"
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
            "inv-cli-export-running",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 40, result.output
    assert not destination.exists()
