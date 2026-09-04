from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
)
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


def _identity_path(project_dir: Path, change_id: str, invocation_id: str) -> Path:
    return (
        project_dir
        / "qa"
        / "changes"
        / change_id
        / ".runtime"
        / "langgraph"
        / "identities"
        / f"{invocation_id}.json"
    )


def _load_identity(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("identity record must be an object")
    return payload


def test_all_fourteen_public_entrypoints_are_current() -> None:
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product import models
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert not hasattr(models, "ENTRYPOINT_RUNTIME_CUTOVER")
    assert set(ENTRYPOINT_AGENT_CONTRACT_IDS) == set(PRODUCT_ENTRYPOINTS)
    assert len(PRODUCT_ENTRYPOINTS) == 14


def test_cli_environment_and_config_cannot_override_cutover(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setenv("AA_RUNTIME", "legacy-v2")
    monkeypatch.setenv("ENTRYPOINT_RUNTIME_CUTOVER", "legacy-v2")
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-cutover-override-001",
        entrypoint="full",
        families=("api",),
    )
    rejected = cli_runner.invoke(app, ["start", *args, "--runtime", "langgraph-v1"])
    assert rejected.exit_code == 2, rejected.output
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    record = _load_identity(_identity_path(project_dir, change_id, "inv-cutover-override-001"))
    assert "runtime" not in record
    assert record["phase"] == "initialized"


def test_production_start_writes_initialized_identity(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-select-langgraph-full-001",
        entrypoint="full",
        families=("api",),
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 0, result.output
    path = _identity_path(project_dir, change_id, "inv-select-langgraph-full-001")
    record = InvocationIdentityRecord.model_validate_json(path.read_bytes())
    assert record.phase == "initialized"
    assert record.entrypoint == "full"
    assert len(record.root_input_digest) == 64
    assert len(record.revision_id) == 64
    assert "runtime" not in record.model_dump(mode="json")


def test_start_writes_current_identity_without_a_selector(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import application
    from assurance_product.cli import app
    from assurance_product.invocation_identity import InvocationIdentityRecord, load_identity
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(application, "_TEST_CRASH_AT", None)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-select-langgraph-001",
        entrypoint="archive",
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 0, result.output
    path = _identity_path(project_dir, change_id, "inv-select-langgraph-001")
    record = InvocationIdentityRecord.model_validate_json(path.read_bytes())
    assert record.phase == "initialized"
    assert record.entrypoint == "archive"
    assert "runtime" not in record.model_dump(mode="json")
    workspace = ChangeWorkspace.open(project_dir.resolve(), change_id)
    assert load_identity(workspace, "inv-select-langgraph-001") == record
    invocation = project_dir / "qa" / "changes" / change_id / ".runtime" / "invocations"
    assert not invocation.exists() or not (invocation / "inv-select-langgraph-001").exists()


def _existing_lifecycle_args(
    args: list[str], project_dir: Path, change_id: str, invocation_id: str
) -> list[str]:
    return [
        "--json",
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        "--product",
        args[args.index("--product") + 1],
        "--binding-dist",
        args[args.index("--binding-dist") + 1],
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        args[args.index("--binding-declaration") + 1],
        "--config-tree",
        args[args.index("--config-tree") + 1],
        "--secret",
        args[args.index("--secret") + 1],
    ]


@pytest.mark.parametrize("crash_at", ["after_initializing", "after_identity", "before_initialized"])
def test_selection_handshake_restart_completes_or_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch, crash_at: str
) -> None:
    from assurance_product import application as runtime_selection
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.application import SelectionCrash

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id=f"inv-crash-{crash_at}",
    )
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", crash_at)
    first = cli_runner.invoke(app, ["start", *args])
    assert first.exit_code == 40, first.output
    assert "SelectionCrash" in first.output or crash_at.replace("_", " ") in first.output.lower()
    path = _identity_path(project_dir, change_id, f"inv-crash-{crash_at}")
    assert path.is_file()
    interrupted = _load_identity(path)
    assert interrupted["phase"] == "initializing"
    assert "runtime" not in interrupted
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
    second = cli_runner.invoke(app, ["start", *args])
    assert second.exit_code == 0, second.output
    completed = _load_identity(path)
    assert completed["phase"] == "initialized"
    assert "runtime" not in completed
    assert completed["entrypoint"] == interrupted["entrypoint"]
    assert completed["root_input_digest"] == interrupted["root_input_digest"]
    assert completed["revision_id"] == interrupted["revision_id"]
    del SelectionCrash


@pytest.mark.parametrize("crash_at", ["after_initializing", "after_identity", "before_initialized"])
def test_aa_run_finishes_interrupted_handshake_before_driving(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch, crash_at: str
) -> None:
    from assurance_product import application as runtime_selection
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.application import SelectionCrash

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id=f"inv-run-crash-{crash_at}",
    )
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", crash_at)
    first = cli_runner.invoke(app, ["run", *args])
    assert first.exit_code == 40, first.output
    path = _identity_path(project_dir, change_id, f"inv-run-crash-{crash_at}")
    interrupted = _load_identity(path)
    assert interrupted["phase"] == "initializing"
    assert "runtime" not in interrupted
    bare = cli_runner.invoke(
        app,
        ["run", *_existing_lifecycle_args(args, project_dir, change_id, f"inv-run-crash-{crash_at}")],
    )
    assert bare.exit_code == 40, bare.output
    assert _load_identity(path)["phase"] == "initializing"
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
    second = cli_runner.invoke(app, ["run", *args])
    assert second.exit_code in {0, 20, 30, 40}, second.output
    completed = _load_identity(path)
    assert completed["phase"] == "initialized"
    assert "runtime" not in completed
    assert completed["entrypoint"] == interrupted["entrypoint"]
    assert completed["root_input_digest"] == interrupted["root_input_digest"]
    del SelectionCrash


def test_leftover_invocation_without_identity_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("seed\n", encoding="utf-8")
    change_id = "CH-LEFTOVER-001"
    ChangeWorkspace.prepare(project_dir, change_id)
    leftover = project_dir / "qa" / "changes" / change_id / ".runtime" / "invocations"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "inv-pre-migration-001").mkdir()
    path = _identity_path(project_dir, change_id, "inv-pre-migration-001")
    assert not path.exists()
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-pre-migration-001",
            "--product",
            "assurance-opencode",
            "--binding-dist",
            installed_sources.deployments["opencode"].distribution,
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            installed_sources.deployments["opencode"].declaration_path,
            "--config-tree",
            str(installed_sources.configuration_tree.path),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output
    assert not path.exists()
    assert leftover.is_dir()


def test_langgraph_run_maps_six_statuses_and_survives_reopen(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine create_engine hook was retired")


def test_langgraph_run_does_not_map_integrity_errors_to_failed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.application import RuntimeSelectionError
    from graph_engine.application import AssuranceApplication

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lg-integrity-001",
        entrypoint="archive",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeSelectionError("checkpoint identity drifted")

    monkeypatch.setattr(AssuranceApplication, "run", boom)
    ran = cli_runner.invoke(
        app,
        [
            "run",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-lg-integrity-001",
            "--product",
            args[args.index("--product") + 1],
            "--binding-dist",
            args[args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            args[args.index("--binding-declaration") + 1],
            "--config-tree",
            args[args.index("--config-tree") + 1],
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert ran.exit_code == 40, ran.output
    assert "checkpoint identity drifted" in ran.output
    assert not ran.stdout.strip() or parse_json_output(ran.stdout).get("status") != "failed"
