from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.preflight import BootstrapPreflightError
from assurance_product.bootstrap.status import run_dir_for, write_bootstrap_status
from tests.product.test_bootstrap_contracts import _spec


def _write_spec(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(_spec().model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def test_bootstrap_run_maps_argv(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app

    captured: dict[str, object] = {}

    def fake_run_bootstrap(**kwargs: object) -> BootstrapStatusV1:
        captured.update(kwargs)
        return BootstrapStatusV1(phase="terminal", change_id="BOOT-1", exit_code=0)

    monkeypatch.setattr("assurance_product.cli.run_bootstrap", fake_run_bootstrap)
    spec_path = _write_spec(tmp_path / "run-spec.yaml")
    project = tmp_path / "sut"
    runs_root = tmp_path / "runs"
    result = cli_runner.invoke(
        app,
        [
            "bootstrap",
            "run",
            "--project-dir",
            str(project),
            "--spec",
            str(spec_path),
            "--runs-root",
            str(runs_root),
            "--change",
            "BOOT-1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["project_dir"] == project
    assert captured["runs_root"] == runs_root
    assert captured["change_id"] == "BOOT-1"
    payload = json.loads(result.stdout)
    assert payload["phase"] == "terminal"
    assert payload["change_id"] == "BOOT-1"


def test_bootstrap_run_exits_40_on_preflight(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app

    def fake_run_bootstrap(**kwargs: object) -> BootstrapStatusV1:
        raise BootstrapPreflightError("policy.yaml missing")

    monkeypatch.setattr("assurance_product.cli.run_bootstrap", fake_run_bootstrap)
    result = cli_runner.invoke(
        app,
        [
            "bootstrap",
            "run",
            "--project-dir",
            str(tmp_path / "sut"),
            "--spec",
            str(_write_spec(tmp_path / "run-spec.yaml")),
            "--runs-root",
            str(tmp_path / "runs"),
            "--json",
        ],
    )
    assert result.exit_code == 40
    assert "policy.yaml" in result.output


def test_bootstrap_run_emits_terminal_error_json_after_runtime_failure(
    cli_runner, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app

    def fake_run_bootstrap(**kwargs: object) -> BootstrapStatusV1:
        runs_root = kwargs["runs_root"]
        change_id = kwargs["change_id"]
        assert isinstance(runs_root, Path)
        assert isinstance(change_id, str)
        write_bootstrap_status(
            run_dir_for(runs_root, change_id),
            BootstrapStatusV1(
                phase="terminal",
                change_id=change_id,
                exit_code=40,
                error="plan-bound Retro source has an incomplete plan binding",
            ),
        )
        raise ValueError("plan-bound Retro source has an incomplete plan binding")

    monkeypatch.setattr("assurance_product.cli.run_bootstrap", fake_run_bootstrap)
    result = cli_runner.invoke(
        app,
        [
            "bootstrap",
            "run",
            "--project-dir",
            str(tmp_path / "sut"),
            "--spec",
            str(_write_spec(tmp_path / "run-spec.yaml")),
            "--runs-root",
            str(tmp_path / "runs"),
            "--change",
            "BOOT-1",
            "--json",
        ],
    )

    assert result.exit_code == 40
    payload = json.loads(result.stdout)
    assert payload["phase"] == "terminal"
    assert payload["exit_code"] == 40
    assert payload["error"] == "plan-bound Retro source has an incomplete plan binding"


def test_bootstrap_run_does_not_emit_stale_terminal_status_when_spec_is_invalid(
    cli_runner, tmp_path: Path
) -> None:
    from assurance_product.cli import app

    runs_root = tmp_path / "runs"
    write_bootstrap_status(
        run_dir_for(runs_root, "BOOT-1"),
        BootstrapStatusV1(phase="terminal", change_id="BOOT-1", exit_code=40, error="previous run failed"),
    )
    result = cli_runner.invoke(
        app,
        [
            "bootstrap",
            "run",
            "--project-dir",
            str(tmp_path / "sut"),
            "--spec",
            str(tmp_path / "missing.yaml"),
            "--runs-root",
            str(runs_root),
            "--change",
            "BOOT-1",
            "--json",
        ],
    )

    assert result.exit_code == 40
    assert result.stdout == ""
    assert "previous run failed" not in result.output


def test_bootstrap_run_does_not_emit_stale_terminal_status_when_run_fails_before_status_write(
    cli_runner, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app

    runs_root = tmp_path / "runs"
    write_bootstrap_status(
        run_dir_for(runs_root, "BOOT-1"),
        BootstrapStatusV1(phase="terminal", change_id="BOOT-1", exit_code=40, error="previous run failed"),
    )

    def fake_run_bootstrap(**kwargs: object) -> BootstrapStatusV1:
        raise ValueError("new run failed before status write")

    monkeypatch.setattr("assurance_product.cli.run_bootstrap", fake_run_bootstrap)
    result = cli_runner.invoke(
        app,
        [
            "bootstrap",
            "run",
            "--project-dir",
            str(tmp_path / "sut"),
            "--spec",
            str(_write_spec(tmp_path / "run-spec.yaml")),
            "--runs-root",
            str(runs_root),
            "--change",
            "BOOT-1",
            "--json",
        ],
    )

    assert result.exit_code == 40
    assert result.stdout == ""
    assert "new run failed before status write" in result.output
    assert "previous run failed" not in result.output
