from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    source_args,
    write_product_input,
    write_project_dir,
)
from tests.product.composition_harness import copy_config_tree, request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


def _existing_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    installed_sources,
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


def _change_runtime(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id / ".runtime"


def test_unknown_entrypoint_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
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
    args, _project_dir, _change_id = common_lifecycle_args(
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
    args, _project_dir, _change_id = common_lifecycle_args(
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
    args, _project_dir, _change_id = common_lifecycle_args(
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


def test_lock_drift_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
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
            "inv-drift-001",
            "--product",
            args[args.index("--product") + 1],
            "--binding-dist",
            args[args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            args[args.index("--binding-declaration") + 1],
            "--config-tree",
            str(tree.path),
            "--secret",
            args[args.index("--secret") + 1],
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


def test_secret_value_is_not_persisted_in_status_or_lock(
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
        invocation_id="inv-secret-persist-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    assert SECRET_VALUE not in started.output
    secret = args[args.index("--secret") + 1]
    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-secret-persist-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    assert SECRET_VALUE not in status.output
    lock = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-secret-persist-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert lock.exit_code == 0, lock.output
    assert SECRET_VALUE not in lock.output
    parse_json_output(status.stdout)
    for path in _change_runtime(project_dir, change_id).rglob("*"):
        if path.is_file():
            assert SECRET_VALUE.encode() not in path.read_bytes()


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


def test_application_resume_file_rejects_unknown_interrupt_id(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-resume-unknown",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output

    async def planted_pending(self: object, **kwargs: object) -> tuple[str, ...]:
        del self, kwargs
        return ("known-interrupt",)

    monkeypatch.setattr(AssuranceProductApplication, "_pending_interrupt_ids", planted_pending)
    resume_file = tmp_path / "unknown-resume.json"
    resume_file.write_text('{"interrupt_id":"unknown","action":"approve"}\n', encoding="utf-8")
    resumed = cli_runner.invoke(
        app,
        [
            "resume",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-resume-unknown",
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
            "--resume-file",
            str(resume_file),
        ],
    )
    assert resumed.exit_code == 40, resumed.output
    assert "unknown interrupt id" in resumed.output.lower()
