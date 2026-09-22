from __future__ import annotations

from pathlib import Path

import pytest

from assurance_product.bootstrap.contracts import BootstrapStatusV1, OpenCodeHandleV1
from assurance_product.bootstrap.driver import run_bootstrap, stop_bootstrap
from assurance_product.bootstrap.opencode import OpenCodeLaunchError
from assurance_product.bootstrap.preflight import BootstrapPreflightError
from assurance_product.bootstrap.status import (
    read_bootstrap_status,
    run_dir_for,
    write_bootstrap_status,
    write_run_manifest,
)
from tests.product.test_bootstrap_contracts import _spec


def _sut(tmp_path: Path) -> Path:
    project = tmp_path / "sut"
    aa = project / ".aa"
    aa.mkdir(parents=True)
    (aa / "policy.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text("version: 1\n", encoding="utf-8")
    return project


def _environ() -> dict[str, str]:
    return {"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"}


def test_run_bootstrap_reaches_terminal(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=4242)
    stopped: list[OpenCodeHandleV1] = []

    def fake_prepare(*, project_dir: Path, run_dir: Path, spec, opencode_endpoint: str, change_id: str):
        del project_dir, spec, change_id
        assert opencode_endpoint == handle.endpoint
        return {
            "product": "assurance-opencode",
            "binding_dist": "assurance-product-bindings-test",
            "binding_declaration": "pkg/assurance-deployment-plugin.json",
            "config_tree": run_dir / "config-tree",
            "input_path": run_dir / "product-input.json",
        }

    status = run_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: handle,
        stop_opencode=stopped.append,
        prepare_composition=fake_prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda url, timeout: None,
    )
    assert status.phase == "terminal"
    assert status.exit_code == 0
    assert status.change_id == "BOOT-1"
    assert stopped == [handle]


def test_run_bootstrap_stops_opencode_on_preflight_failure(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    (project / ".aa" / "policy.yaml").unlink()
    started: list[object] = []
    with pytest.raises(BootstrapPreflightError, match="policy"):
        run_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=tmp_path / "runs",
            change_id="BOOT-1",
            environ=_environ(),
            start_opencode_serve=lambda **kwargs: (
                started.append(kwargs)
                or OpenCodeHandleV1(
                    endpoint="http://127.0.0.1:4101",
                    pid=1,
                )
            ),
            stop_opencode=lambda handle: None,
            prepare_composition=lambda **kwargs: {},
            wait_ready=lambda url, timeout: None,
        )
    assert started == []


def test_run_bootstrap_marks_terminal_when_opencode_launch_fails(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    runs_root = tmp_path / "runs"

    def fail_start(**kwargs: object) -> OpenCodeHandleV1:
        del kwargs
        raise OpenCodeLaunchError("OpenCode agent target already has different content")

    with pytest.raises(OpenCodeLaunchError, match="already has different content"):
        run_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=runs_root,
            change_id="BOOT-1",
            environ=_environ(),
            start_opencode_serve=fail_start,
            stop_opencode=lambda handle: None,
            prepare_composition=lambda **kwargs: {},
            wait_ready=lambda url, timeout: None,
        )
    status = read_bootstrap_status(run_dir_for(runs_root, "BOOT-1"))
    assert status.phase == "terminal"
    assert status.exit_code == 40
    assert status.error is not None
    assert "already has different content" in status.error


def test_run_bootstrap_marks_terminal_when_running_graph_raises_value_error(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    runs_root = tmp_path / "runs"
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=4242)
    stopped: list[OpenCodeHandleV1] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        return {
            "product": "assurance-opencode",
            "binding_dist": "assurance-product-bindings-test",
            "binding_declaration": "pkg/assurance-deployment-plugin.json",
            "config_tree": run_dir / "config-tree",
            "input_path": run_dir / "product-input.json",
        }

    def fail_run(**kwargs: object) -> tuple[object, str]:
        del kwargs
        raise ValueError("plan-bound Retro source has an incomplete plan binding")

    with pytest.raises(ValueError, match="incomplete plan binding"):
        run_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=runs_root,
            change_id="BOOT-1",
            environ=_environ(),
            start_opencode_serve=lambda **kwargs: handle,
            stop_opencode=stopped.append,
            prepare_composition=prepare,
            start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
            run_invocation=fail_run,
            read_status=lambda **kwargs: {"status": "running"},
            wait_ready=lambda url, timeout: None,
        )

    status = read_bootstrap_status(run_dir_for(runs_root, "BOOT-1"))
    assert status.phase == "terminal"
    assert status.exit_code == 40
    assert status.error is not None and "incomplete plan binding" in status.error
    assert stopped == [handle]


def test_stop_bootstrap_kills_recorded_pid(tmp_path: Path) -> None:
    run_dir = run_dir_for(tmp_path / "runs", "BOOT-1")
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4100", pid=4242)
    write_run_manifest(run_dir, {"project_dir": str((tmp_path / "sut").resolve())})
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(phase="running", change_id="BOOT-1", opencode=handle),
    )
    killed: list[OpenCodeHandleV1] = []
    status = stop_bootstrap(run_dir, stop_opencode=killed.append)
    assert killed == [handle]
    assert status.phase == "terminal"
    assert status.exit_code == 20
