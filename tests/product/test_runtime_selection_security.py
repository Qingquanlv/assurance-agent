from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.product.cli_support import SECRET_ENV, SECRET_VALUE, common_lifecycle_args
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _reset_handshake_crash() -> Iterator[None]:
    yield
    from assurance_product import application

    application._TEST_CRASH_AT = None


def _runtime_root(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id / ".runtime"


def _identity_path(project_dir: Path, change_id: str, invocation_id: str) -> Path:
    return _runtime_root(project_dir, change_id) / "langgraph" / "identities" / f"{invocation_id}.json"


def test_initializing_record_is_not_resumable(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import application
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-init-not-resumable",
    )
    monkeypatch.setattr(application, "_TEST_CRASH_AT", "after_initializing")
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 40, started.output
    monkeypatch.setattr(application, "_TEST_CRASH_AT", None)
    resume = cli_runner.invoke(
        app,
        [
            "resume",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-init-not-resumable",
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
            "--action",
            "approve",
            "--reason",
            "accepted",
        ],
    )
    assert resume.exit_code == 40, resume.output


def test_corrupt_selection_record_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-corrupt-selection",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    path = _identity_path(project_dir, change_id, "inv-corrupt-selection")
    path.write_text("{not-json", encoding="utf-8")
    status = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-corrupt-selection",
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
    assert status.exit_code == 40, status.output


def test_selection_symlink_is_rejected(cli_runner, installed_sources, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-symlink-selection",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    path = _identity_path(project_dir, change_id, "inv-symlink-selection")
    backup = path.read_bytes()
    path.unlink()
    target = path.with_name("inv-symlink-selection.real.json")
    target.write_bytes(backup)
    path.symlink_to(target)
    status = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-symlink-selection",
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
    assert status.exit_code == 40, status.output


def test_unknown_control_entry_fails_closed_before_mutation(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-unknown-control",
        entrypoint="full",
        families=("api",),
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    leftover = _runtime_root(project_dir, change_id) / "invocations"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "inv-unknown-control").mkdir()
    status = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-unknown-control",
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
    assert status.exit_code == 40, status.output
    assert leftover.is_dir()


def test_absent_legacy_evidence_without_marker_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("seed\n", encoding="utf-8")
    ChangeWorkspace.prepare(project_dir, "CH-ABSENT-001")
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-ABSENT-001",
            "--invocation-id",
            "inv-absent-001",
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


def test_record_evidence_disagreement_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-disagree-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    path = _identity_path(project_dir, change_id, "inv-disagree-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revision_id"] = "0" * 64
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    status = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-disagree-001",
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
    assert status.exit_code == 40, status.output


@pytest.mark.parametrize("field", ["revision_id", "product_lock_digest", "root_input_digest"])
def test_langgraph_initialized_record_disagrees_with_checkpoint_evidence(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch, field: str
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    invocation_id = f"inv-lg-disagree-{field}"
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id=invocation_id,
        entrypoint="archive",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    path = _identity_path(project_dir, change_id, invocation_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = "0" * 64
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    status = cli_runner.invoke(
        app,
        [
            "status",
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
        ],
    )
    assert status.exit_code == 40, status.output


def test_cutover_validator_and_runtime_selector_are_gone() -> None:
    import importlib.util

    from assurance_product import application
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert importlib.util.find_spec("assurance_product.runtime_selection") is None
    assert not hasattr(application, "select_runtime")
    assert not hasattr(application, "use_test_runtime_selector")
    assert not hasattr(application, "validate_entrypoint_runtime_cutover")
    assert set(PRODUCT_ENTRYPOINTS) == set(application.ENTRYPOINT_AGENT_CONTRACT_IDS)


def test_reopen_ignores_current_switch_during_initializing(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import application
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-reopen-switch",
        entrypoint="archive",
    )
    monkeypatch.setattr(application, "_TEST_CRASH_AT", "after_initializing")
    crashed = cli_runner.invoke(app, ["start", *args])
    assert crashed.exit_code == 40, crashed.output
    payload = json.loads(_identity_path(project_dir, change_id, "inv-reopen-switch").read_text())
    assert "runtime" not in payload
    assert payload["phase"] == "initializing"
    monkeypatch.setattr(application, "_TEST_CRASH_AT", None)
    restarted = cli_runner.invoke(app, ["start", *args])
    assert restarted.exit_code == 0, restarted.output
    completed = json.loads(_identity_path(project_dir, change_id, "inv-reopen-switch").read_text())
    assert "runtime" not in completed
    assert completed["phase"] == "initialized"
