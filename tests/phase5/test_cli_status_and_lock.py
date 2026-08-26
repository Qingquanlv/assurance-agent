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
from tests.phase5.composition_harness import InstalledSources, request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


def _status_schema() -> dict[str, object]:
    raw = files("assurance_product").joinpath("resources/schemas/status-v1.json").read_bytes()
    return json.loads(raw.decode("utf-8"))


def _existing_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    installed_sources: InstalledSources,
    secret: str,
) -> list[str]:
    return [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--secret",
        secret,
        "--json",
    ]


def test_status_json_matches_schema_after_start(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.models import StatusV1
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-status-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    result = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-status-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    status = StatusV1.model_validate(document)
    assert status.schema_version == "1"
    assert status.invocation_id == "inv-status-001"
    assert status.lock_digest == composition.lock_digest
    assert status.entrypoint == "intake"
    assert status.change.change_id == change_id
    assert status.status in {"running", "blocked", "interrupted", "stopped", "failed", "completed"}
    assert "initial_tree_id" not in document
    assert "current_head_tree_id" not in document
    schema = _status_schema()
    assert schema["title"] == "StatusV1"
    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    for key in required:
        assert key in document


def test_render_status_projects_started_invocation(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.status import render_status
    from graph_engine.runtime.engine import Engine
    from graph_engine.runtime.ledger import Ledger
    from graph_engine.runtime.models import fold_events
    from graph_engine.runtime.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-render-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    secrets = (
        SecretSourceBinding(handle="opencode.token", source_kind="environment", source_locator=SECRET_ENV),
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=secrets,
        digest=runtime_authorization_digest(secrets),
    )
    workspace = ChangeWorkspace.open(project_dir, change_id)
    with Engine(workspace.paths.runtime_root, host=None) as engine:
        with engine.open(
            "inv-render-001",
            composition,
            authorization=authorization,
            workspace_binding=workspace.runtime_binding(),
        ) as handle:
            envelopes = Ledger(handle.invocation_root / "ledger").read_all()
            projection = fold_events(envelopes)
            start_doc = parse_json_output(started.stdout)
            status = render_status(
                projection,
                root_input_digest=start_doc["root_input_digest"],
                change_id=change_id,
            )
    assert status.invocation_id == "inv-render-001"
    assert status.lock_digest == composition.lock_digest
    assert status.change.change_id == change_id
    assert "seed_tree_id" not in start_doc
    assert "current_head_tree_id" not in status.model_dump(mode="json")
    assert status.graph_hierarchy


def test_lock_show_prints_authenticated_closed_projection(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lock-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    result = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-lock-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["lock_digest"] == composition.lock_digest
    assert document["engine_api"] == "2.0"
    lock = document["lock"]
    assert lock["digest"] == composition.lock.digest
    assert "canonical_bytes" not in lock


def test_status_after_run_is_authoritative_completed_projection(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.models import StatusV1
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-status-run-001",
    )
    run = cli_runner.invoke(app, ["run", *args])
    assert run.exit_code == 0, run.output
    result = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-status-run-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    status = StatusV1.model_validate(parse_json_output(result.stdout))
    assert status.status == "completed"
    assert status.entrypoint == "intake"
    assert status.change.change_id == change_id
    assert status.pending_interrupt is None
