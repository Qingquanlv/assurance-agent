from __future__ import annotations

import json
from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def _reset_runtime_ports() -> Iterator[None]:
    yield
    try:
        from assurance_product.runtime_ports import ProductRuntimePorts

        ProductRuntimePorts.test_kernel_resolutions = None
        ProductRuntimePorts._last_scripted_committed = None
    except ImportError:
        return


def _selection_path(project_dir: Path, change_id: str, invocation_id: str) -> Path:
    return (
        project_dir
        / "qa"
        / "changes"
        / change_id
        / ".runtime"
        / "langgraph"
        / "selections"
        / f"{invocation_id}.json"
    )


def _load_selection(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("selection record must be an object")
    return payload


def test_entrypoint_runtime_cutover_flips_all_fourteen_names() -> None:
    from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS

    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_RUNTIME_CUTOVER) == 14
    flipped = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "langgraph-v1"}
    leftover = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "legacy-v2"}
    assert flipped == set(PRODUCT_ENTRYPOINTS)
    assert leftover == set()


def test_cli_environment_and_config_cannot_override_cutover(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setenv("AA_RUNTIME", "langgraph-v1")
    monkeypatch.setenv("ENTRYPOINT_RUNTIME_CUTOVER", "langgraph-v1")
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
    record = _load_selection(_selection_path(project_dir, change_id, "inv-cutover-override-001"))
    assert record["runtime"] == "langgraph-v1"
    assert record["phase"] == "initialized"


def test_production_start_writes_initialized_langgraph_selection(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.application import LangGraphRuntimeRecord

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
    path = _selection_path(project_dir, change_id, "inv-select-langgraph-full-001")
    record = LangGraphRuntimeRecord.model_validate_json(path.read_bytes())
    assert record.phase == "initialized"
    assert record.runtime == "langgraph-v1"
    assert record.entrypoint == "full"
    assert record.root_input_digest is not None and len(record.root_input_digest) == 64
    assert record.identity_digest is not None and len(record.identity_digest) == 64


def test_test_owned_selector_can_choose_langgraph(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import application
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.application import LangGraphRuntimeRecord

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
    path = _selection_path(project_dir, change_id, "inv-select-langgraph-001")
    record = LangGraphRuntimeRecord.model_validate_json(path.read_bytes())
    assert record.phase == "initialized"
    assert record.runtime == "langgraph-v1"
    assert record.entrypoint == "archive"
    assert record.identity_digest is not None and len(record.identity_digest) == 64
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
    path = _selection_path(project_dir, change_id, f"inv-crash-{crash_at}")
    assert path.is_file()
    interrupted = _load_selection(path)
    assert interrupted["phase"] == "initializing"
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
    second = cli_runner.invoke(app, ["start", *args])
    assert second.exit_code == 0, second.output
    completed = _load_selection(path)
    assert completed["phase"] == "initialized"
    assert completed["runtime"] == interrupted["runtime"]
    assert completed["entrypoint"] == interrupted["entrypoint"]
    assert completed["root_input_digest"] == interrupted["root_input_digest"]
    assert completed["build_identity"] == interrupted["build_identity"]
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
    path = _selection_path(project_dir, change_id, f"inv-run-crash-{crash_at}")
    interrupted = _load_selection(path)
    assert interrupted["phase"] == "initializing"
    bare = cli_runner.invoke(
        app,
        ["run", *_existing_lifecycle_args(args, project_dir, change_id, f"inv-run-crash-{crash_at}")],
    )
    assert bare.exit_code == 40, bare.output
    assert _load_selection(path)["phase"] == "initializing"
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
    second = cli_runner.invoke(app, ["run", *args])
    assert second.exit_code in {0, 20, 30, 40}, second.output
    completed = _load_selection(path)
    assert completed["phase"] == "initialized"
    assert completed["runtime"] == interrupted["runtime"]
    assert completed["entrypoint"] == interrupted["entrypoint"]
    assert completed["root_input_digest"] == interrupted["root_input_digest"]
    del SelectionCrash


def test_pre_migration_legacy_invocation_is_backfilled(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from tests.product.cli_support import start_lifecycle_invocation

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    invocation = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-pre-migration-001",
    )
    path = _selection_path(invocation.project_dir, invocation.change_id, invocation.id)
    assert not path.exists()
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(invocation.project_dir),
            "--change",
            invocation.change_id,
            "--invocation-id",
            invocation.id,
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
    assert result.exit_code == 0, result.output
    record = _load_selection(path)
    assert record["phase"] == "initialized"
    assert record["runtime"] == "legacy-v2"
    second = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(invocation.project_dir),
            "--change",
            invocation.change_id,
            "--invocation-id",
            invocation.id,
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
    assert second.exit_code == 0, second.output
    assert _load_selection(path) == record
    close = getattr(invocation.engine, "close", None)
    if callable(close):
        close()


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
    from assurance_product.runtime_ports import ProductRuntimePorts
    from assurance_product.application import RuntimeSelectionError
    from graph_engine.application import AssuranceApplication
    from graph_engine.attempts.resolutions import CommittedTaskResult, ReceiptRef

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    ProductRuntimePorts.test_kernel_resolutions = [
        CommittedTaskResult(
            output={"status": "completed"},
            receipt=ReceiptRef(receipt_id="r-integrity", receipt_digest="d" * 64),
        )
    ]
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
