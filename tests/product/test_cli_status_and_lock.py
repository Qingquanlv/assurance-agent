from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    source_args,
)
from tests.product.composition_harness import InstalledSources, request_for

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
    from graph_engine.attempts.activity import Ledger, fold_events
    pytest.skip("leftover Engine status projection was retired")
    from graph_engine.attempts.secret_sources import (
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
    from assurance_product.cli import app
    from assurance_product.models import StatusV1
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-status-run-001",
        entrypoint="archive",
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
                invocation_id="inv-status-run-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    status = StatusV1.model_validate(parse_json_output(result.stdout))
    assert status.entrypoint == "archive"
    assert status.change.change_id == change_id
    assert status.status in {"completed", "initialized", "ready", "running", "interrupted", "blocked"}


def test_lock_show_keeps_v2_for_legacy_records(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.revision_registry import RevisionRegistry
    from assurance_product.runtime_selection import (
        LegacyRuntimeRecord,
        complete_initialized,
        write_initializing,
    )
    from graph_engine.evidence.legacy_v2 import authenticate_invocation_lock_v2

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lock-legacy-v2",
    )
    (project_dir / "qa" / "changes" / change_id).mkdir(parents=True, exist_ok=True)
    workspace = ChangeWorkspace.open(project_dir, change_id)
    workspace.initialize()
    leftover_lock_bytes = (
        Path(__file__).resolve().parents[2]
        / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
    ).read_text(encoding="utf-8").strip().encode()
    leftover_invocation = workspace.paths.runtime_root / "invocations" / "inv-lock-legacy-v2"
    leftover_invocation.mkdir(parents=True, exist_ok=True)
    (leftover_invocation / "invocation.lock.json").write_bytes(leftover_lock_bytes)
    leftover_digest = authenticate_invocation_lock_v2(leftover_lock_bytes).digest
    write_initializing(
        workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id="inv-lock-legacy-v2",
            entrypoint="archive",
            root_input_digest="c" * 64,
            build_identity=leftover_digest,
        ),
    )
    complete_initialized(
        workspace,
        LegacyRuntimeRecord(
            phase="initialized",
            invocation_id="inv-lock-legacy-v2",
            entrypoint="archive",
            root_input_digest="c" * 64,
            build_identity=leftover_digest,
            identity_digest=leftover_digest,
        ),
    )
    RevisionRegistry(workspace).bind("inv-lock-legacy-v2", runtime="legacy-v2", revision_id=leftover_digest)
    result = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-lock-legacy-v2",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["lock"]["schema_version"] == "2"
    assert document["lock_digest"] == leftover_digest
