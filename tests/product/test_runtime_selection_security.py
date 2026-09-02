from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.product.cli_support import SECRET_ENV, SECRET_VALUE, common_lifecycle_args
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _reset_runtime_selector() -> None:
    yield
    try:
        from assurance_product.runtime_selection import use_test_runtime_selector

        use_test_runtime_selector(None)
    except ImportError:
        return


def _runtime_root(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id / ".runtime"


def _selection_path(project_dir: Path, change_id: str, invocation_id: str) -> Path:
    return _runtime_root(project_dir, change_id) / "langgraph" / "selections" / f"{invocation_id}.json"


def test_initializing_record_is_not_resumable(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import runtime_selection
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
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", "after_initializing")
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 40, started.output
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
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
    path = _selection_path(project_dir, change_id, "inv-corrupt-selection")
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
    path = _selection_path(project_dir, change_id, "inv-symlink-selection")
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


def test_both_runtime_artifacts_fail_closed(
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
        invocation_id="inv-both-runtime",
        entrypoint="full",
        families=("api",),
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    leftover_lock = (
        _runtime_root(project_dir, change_id) / "invocations" / "inv-both-runtime" / "invocation.lock.json"
    )
    leftover_lock.parent.mkdir(parents=True, exist_ok=True)
    leftover_lock.write_text("{}\n", encoding="utf-8")
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
            "inv-both-runtime",
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
    path = _selection_path(project_dir, change_id, "inv-disagree-001")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["identity_digest"] = "0" * 64
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


@pytest.mark.parametrize("field", ["identity_digest", "build_identity", "root_input_digest"])
def test_langgraph_initialized_record_disagrees_with_checkpoint_evidence(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch, field: str
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_selection import use_test_runtime_selector

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    use_test_runtime_selector(lambda _entrypoint: "langgraph-v1")
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
    path = _selection_path(project_dir, change_id, invocation_id)
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


def test_cutover_missing_or_extra_keys_fail_boot() -> None:
    from graph_engine.boot.boot import BootValidationError

    from assurance_product.models import PRODUCT_ENTRYPOINTS
    from assurance_product.runtime_selection import validate_entrypoint_runtime_cutover

    complete = {name: "legacy-v2" for name in PRODUCT_ENTRYPOINTS}
    missing = {name: kind for name, kind in complete.items() if name != "intake"}
    extra = {**complete, "shadow": "langgraph-v1"}
    with pytest.raises(BootValidationError, match="missing"):
        validate_entrypoint_runtime_cutover(missing)
    with pytest.raises(BootValidationError, match="extra"):
        validate_entrypoint_runtime_cutover(extra)
    validate_entrypoint_runtime_cutover(complete)


def test_reopen_ignores_current_switch_during_initializing(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import runtime_selection
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_selection import use_test_runtime_selector

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    use_test_runtime_selector(lambda _entrypoint: "langgraph-v1")
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-reopen-switch",
        entrypoint="archive",
    )
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", "after_initializing")
    crashed = cli_runner.invoke(app, ["start", *args])
    assert crashed.exit_code == 40, crashed.output
    payload = json.loads(_selection_path(project_dir, change_id, "inv-reopen-switch").read_text())
    assert payload["runtime"] == "langgraph-v1"
    assert payload["phase"] == "initializing"
    monkeypatch.setattr(runtime_selection, "_TEST_CRASH_AT", None)
    use_test_runtime_selector(lambda _entrypoint: "legacy-v2")
    restarted = cli_runner.invoke(app, ["start", *args])
    assert restarted.exit_code == 0, restarted.output
    completed = json.loads(_selection_path(project_dir, change_id, "inv-reopen-switch").read_text())
    assert completed["runtime"] == "langgraph-v1"
    assert completed["phase"] == "initialized"
