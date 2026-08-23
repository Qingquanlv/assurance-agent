from __future__ import annotations

from pathlib import Path
import threading

import pytest

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    scripted_engine_factory,
    source_args,
    write_product_input,
    write_project_dir,
)
from tests.phase5.composition_harness import copy_config_tree, request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_unknown_entrypoint_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-unknown-001",
        entrypoint="not-an-entrypoint",
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_wrong_runtime_vs_product_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-wrong-runtime-001",
    )
    product_index = args.index("--product") + 1
    args[product_index] = "assurance-cursor"
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_forged_config_fails_closed_on_start(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-forged-001",
    )
    tree = copy_config_tree(tmp_path / "forged-config")
    (tree.path / "undeclared.txt").write_text("forged", encoding="utf-8")
    args[args.index("--config-tree") + 1] = str(tree.path)
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_invalid_input_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-bad-input-001",
        extra={"selected_test_families": ("api",)},
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_missing_secret_handle_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project_dir = write_project_dir(tmp_path / "project")
    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    input_path = write_product_input(tmp_path / "input.json", composition)
    result = cli_runner.invoke(
        app,
        [
            "start",
            "--json",
            "--project-dir",
            str(project_dir),
            "--engine-root",
            str(engine_root),
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
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--engine-root",
            str(engine_root),
            "--invocation-id",
            "inv-missing-001",
            *source_args(installed_sources),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output


def test_lock_drift_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-drift-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    tree = copy_config_tree(tmp_path / "drifted-config")
    policy = tree.path / ".aa" / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
    drifted = list(args)
    drifted[drifted.index("--config-tree") + 1] = str(tree.path)
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--engine-root",
            drifted[drifted.index("--engine-root") + 1],
            "--invocation-id",
            "inv-drift-001",
            "--product",
            drifted[drifted.index("--product") + 1],
            "--binding-dist",
            drifted[drifted.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            drifted[drifted.index("--binding-declaration") + 1],
            "--config-tree",
            str(tree.path),
            "--secret",
            drifted[drifted.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 40, result.output


def test_wrong_authorization_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setenv("AA_NEXT_OTHER_TOKEN", "other")
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-auth-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--engine-root",
            args[args.index("--engine-root") + 1],
            "--invocation-id",
            "inv-auth-001",
            *source_args(installed_sources),
            "--secret",
            "opencode.token=env:AA_NEXT_OTHER_TOKEN",
        ],
    )
    assert result.exit_code == 40, result.output


def test_invalid_resume_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
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
        invocation_id="inv-resume-bad-001",
    )
    run = cli_runner.invoke(app, ["run", *args])
    assert run.exit_code == 0, run.output
    result = cli_runner.invoke(
        app,
        [
            "resume",
            "--json",
            "--action",
            "approve",
            "--reason",
            "accepted",
            "--engine-root",
            args[args.index("--engine-root") + 1],
            "--invocation-id",
            "inv-resume-bad-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 40, result.output


def test_active_run_conflict_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from graph_engine.runtime.engine import Engine
    from graph_engine.runtime.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-conflict-001",
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
    held = threading.Event()
    release = threading.Event()

    def hold_claim() -> None:
        with Engine(engine_root) as engine:
            handle = engine.open("inv-conflict-001", composition, authorization=authorization)
            try:
                claim = engine._acquire_runner_claim(handle._invocation_fd)
                held.set()
                release.wait(timeout=10)
                os_close = __import__("os").close
                os_close(claim)
            finally:
                handle.close()

    worker = threading.Thread(target=hold_claim, name="hold-claim")
    worker.start()
    assert held.wait(timeout=10)
    try:
        result = cli_runner.invoke(
            app,
            [
                "run",
                "--json",
                "--engine-root",
                str(engine_root),
                "--invocation-id",
                "inv-conflict-001",
                *source_args(installed_sources),
                "--secret",
                args[args.index("--secret") + 1],
                "--project-dir",
                args[args.index("--project-dir") + 1],
                "--entrypoint",
                "intake",
                "--input",
                args[args.index("--input") + 1],
            ],
        )
        assert result.exit_code == 40, result.output
    finally:
        release.set()
        worker.join(timeout=10)


def test_secret_value_is_not_persisted_in_status_or_lock(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, engine_root = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-secret-persist-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    assert SECRET_VALUE not in started.output
    status = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--engine-root",
            str(engine_root),
            "--invocation-id",
            "inv-secret-persist-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert status.exit_code == 0, status.output
    assert SECRET_VALUE not in status.output
    lock = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            "--json",
            "--engine-root",
            str(engine_root),
            "--invocation-id",
            "inv-secret-persist-001",
            *source_args(installed_sources),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert lock.exit_code == 0, lock.output
    assert SECRET_VALUE not in lock.output
    parse_json_output(status.stdout)
    for path in (engine_root).rglob("*"):
        if path.is_file():
            assert SECRET_VALUE.encode() not in path.read_bytes()


def test_binding_entrypoint_must_be_deployment(cli_runner, installed_sources):
    from assurance_product.cli import app

    args = source_args(installed_sources)
    args[args.index("--binding-entrypoint") + 1] = "wildcard"
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code in {2, 40}, result.output
