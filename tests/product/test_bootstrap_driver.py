from __future__ import annotations

from pathlib import Path

import pytest

from assurance_product.bootstrap.contracts import BootstrapStatusV1, OpenCodeHandleV1
from assurance_product.bootstrap.driver import resume_bootstrap, run_bootstrap, stop_bootstrap
from assurance_product.bootstrap.opencode import OpenCodeLaunchError
from assurance_product.bootstrap.preflight import BootstrapPreflightError
from assurance_product.bootstrap.status import (
    read_bootstrap_status,
    run_dir_for,
    write_bootstrap_status,
    write_run_manifest,
    write_stop_request,
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

    def fake_prepare(
        *,
        project_dir: Path,
        run_dir: Path,
        spec,
        opencode_endpoint: str,
        change_id: str,
        parent_session_id: str | None = None,
    ):
        del project_dir, spec, change_id, parent_session_id
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


def test_stop_bootstrap_does_not_kill_a_shared_server(tmp_path: Path) -> None:
    run_dir = run_dir_for(tmp_path / "runs", "BOOT-1")
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4100", pid=4242)
    write_run_manifest(run_dir, {"project_dir": str(tmp_path / "sut"), "ownership": "shared"})
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(phase="running", change_id="BOOT-1", opencode=handle),
    )
    killed: list[OpenCodeHandleV1] = []
    status = stop_bootstrap(run_dir, stop_opencode=killed.append)
    assert killed == []
    assert status.phase == "terminal"
    assert (run_dir / "stop-request.json").is_file()


def test_run_bootstrap_stops_after_a_cancellation_request(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=4242)
    runs_root = tmp_path / "runs"
    calls = {"run": 0}

    def run_invocation(**kwargs: object) -> tuple[dict[str, str], str]:
        del kwargs
        calls["run"] += 1
        write_stop_request(run_dir_for(runs_root, "BOOT-1"), change_id="BOOT-1")
        return {"status": "running"}, "running"

    status = run_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=runs_root,
        change_id="BOOT-1",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: handle,
        stop_opencode=lambda handle: None,
        prepare_composition=lambda **kwargs: {
            "product": "assurance-opencode",
            "binding_dist": "assurance-product-bindings-test",
            "binding_declaration": "pkg/assurance-deployment-plugin.json",
            "config_tree": runs_root / "config-tree",
            "input_path": runs_root / "product-input.json",
        },
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=run_invocation,
        read_status=lambda **kwargs: {"status": "running"},
        wait_ready=lambda url, timeout: None,
    )
    assert calls["run"] == 1
    assert status.phase == "terminal"
    assert status.exit_code == 20
    assert status.status["status"] == "stopped"


def _prepared(run_dir: Path) -> dict[str, object]:
    return {
        "product": "assurance-opencode",
        "binding_dist": "assurance-product-bindings-test",
        "binding_declaration": "pkg/assurance-deployment-plugin.json",
        "config_tree": run_dir / "config-tree",
        "input_path": run_dir / "product-input.json",
    }


def test_run_bootstrap_attaches_a_shared_server_and_does_not_stop_it(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    stopped: list[OpenCodeHandleV1] = []
    parents: list[str | None] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        parent = kwargs.get("parent_session_id")
        parents.append(parent if isinstance(parent, str) or parent is None else None)
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        return _prepared(run_dir)

    def ready(url: str, timeout: float, authorization: str | None = None) -> None:
        del timeout, authorization
        if url.endswith("/global/health"):
            return

    status = run_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: (_ for _ in ()).throw(AssertionError("private start")),
        stop_opencode=stopped.append,
        prepare_composition=prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=ready,
        shared_endpoint="http://127.0.0.1:4096",
        ensure_run_root=lambda **kwargs: "ses_root",
    )
    assert status.phase == "terminal"
    assert status.exit_code == 0
    assert status.root_session_id == "ses_root"
    assert status.opencode is not None
    assert status.opencode.ownership == "shared"
    assert status.opencode.pid is None
    assert parents == ["ses_root"]
    assert stopped == []


def test_shared_attach_failure_is_an_explicit_terminal_failure(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    stopped: list[OpenCodeHandleV1] = []

    def ready(url: str, timeout: float, authorization: str | None = None) -> None:
        del timeout, authorization
        if url.endswith("/global/health"):
            raise OpenCodeLaunchError("connection refused")

    status = run_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: (_ for _ in ()).throw(AssertionError("private start")),
        stop_opencode=stopped.append,
        prepare_composition=lambda **kwargs: {},
        wait_ready=ready,
        shared_endpoint="http://127.0.0.1:4096",
    )
    assert status.phase == "terminal"
    assert status.exit_code == 30
    assert status.error is not None and "connection refused" in status.error
    assert stopped == []


def test_shared_server_loss_interrupts_before_another_invocation(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    stopped: list[OpenCodeHandleV1] = []
    health = {"calls": 0}

    def ready(url: str, timeout: float, authorization: str | None = None) -> None:
        del timeout, authorization
        if not url.endswith("/global/health"):
            return
        health["calls"] += 1
        if health["calls"] > 1:
            raise OpenCodeLaunchError("server gone")

    status = run_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: (_ for _ in ()).throw(AssertionError("private start")),
        stop_opencode=stopped.append,
        prepare_composition=lambda **kwargs: _prepared(tmp_path),
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("invocation")),
        read_status=lambda **kwargs: {"status": "running"},
        wait_ready=ready,
        shared_endpoint="http://127.0.0.1:4096",
    )
    assert status.exit_code == 30
    assert status.error is not None and "unavailable" in status.error
    assert stopped == []


def test_resume_bootstrap_reattaches_a_shared_server(tmp_path: Path) -> None:
    from assurance_product.bootstrap.status import write_effective_spec

    project = _sut(tmp_path)
    run_dir = run_dir_for(tmp_path / "runs", "BOOT-1")
    write_effective_spec(run_dir, _spec())
    write_run_manifest(
        run_dir,
        {
            "project_dir": str(project.resolve()),
            "ownership": "shared",
            "requested_opencode_endpoint": "http://127.0.0.1:4096",
        },
    )
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(
            phase="terminal",
            change_id="BOOT-1",
            root_session_id="ses_root",
            exit_code=20,
            opencode=OpenCodeHandleV1(endpoint="http://127.0.0.1:4096", ownership="shared"),
        ),
    )
    stopped: list[OpenCodeHandleV1] = []
    parents: list[object] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        parents.append(kwargs.get("parent_session_id"))
        return _prepared(run_dir)

    status = resume_bootstrap(
        run_dir,
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: (_ for _ in ()).throw(AssertionError("private start")),
        stop_opencode=stopped.append,
        prepare_composition=prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda url, timeout, authorization=None: None,
    )
    assert status.exit_code == 0
    assert status.opencode is not None and status.opencode.ownership == "shared"
    assert parents == ["ses_root"]
    assert stopped == []
