from __future__ import annotations

import json
import subprocess
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


@pytest.fixture(autouse=True)
def current_product_imports(monkeypatch):
    # installed_sources replaces collected source modules with extracted wheels.
    # Keep decorated functions and their exception/model identities coherent.
    from assurance_product.bootstrap import contracts, driver, opencode, preflight, status

    for module, names in (
        (contracts, ("BootstrapStatusV1", "OpenCodeHandleV1")),
        (driver, ("resume_bootstrap", "run_bootstrap", "stop_bootstrap")),
        (opencode, ("OpenCodeLaunchError",)),
        (preflight, ("BootstrapPreflightError",)),
        (
            status,
            (
                "read_bootstrap_status",
                "run_dir_for",
                "write_bootstrap_status",
                "write_run_manifest",
                "write_stop_request",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setitem(globals(), name, getattr(module, name))


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


def test_stop_bootstrap_waits_for_checkpoint_before_stopping_private_server(tmp_path: Path) -> None:
    run_dir = run_dir_for(tmp_path / "runs", "BOOT-1")
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4100", pid=4242)
    write_run_manifest(run_dir, {"project_dir": str((tmp_path / "sut").resolve())})
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(phase="running", change_id="BOOT-1", opencode=handle),
    )
    killed: list[OpenCodeHandleV1] = []
    status = stop_bootstrap(run_dir, stop_opencode=killed.append)
    assert killed == []
    assert status.phase == "running"
    assert status.exit_code is None
    assert (run_dir / "stop-request.json").is_file()


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
    assert status.phase == "running"
    assert (run_dir / "stop-request.json").is_file()


def test_run_bootstrap_stops_after_a_cancellation_request(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=4242)
    runs_root = tmp_path / "runs"
    calls = {"run": 0}

    def run_invocation(**kwargs: object) -> tuple[dict[str, str], str]:
        assert kwargs["stop_file"] == run_dir_for(runs_root, "BOOT-1") / "stop-request.json"
        calls["run"] += 1
        write_stop_request(run_dir_for(runs_root, "BOOT-1"), change_id="BOOT-1")
        return {"status": "blocked"}, "blocked"

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
        read_status=lambda **kwargs: {
            "status": "blocked",
            "pending_interrupt": {"reason_category": "operator_stop"},
        },
        wait_ready=lambda url, timeout: None,
    )
    assert calls["run"] == 1
    assert status.phase == "terminal"
    assert status.exit_code == 20
    assert status.status["status"] == "blocked"


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
        ensure_run_root=lambda **kwargs: (_ for _ in ()).throw(AssertionError("root session")),
    )
    assert status.phase == "terminal"
    assert status.exit_code == 0
    assert status.root_session_id is None
    assert status.opencode is not None
    assert status.opencode.ownership == "shared"
    assert status.opencode.pid is None
    assert parents == [None]
    assert stopped == []


def test_managed_run_does_not_create_a_root_session(tmp_path: Path) -> None:
    task = _sut(tmp_path)
    parents: list[object] = []
    disposed: list[dict[str, object]] = []

    def record_dispose(**kwargs: object) -> None:
        disposed.append(dict(kwargs))

    def forbidden_root(**kwargs: object) -> str:
        del kwargs
        raise AssertionError("managed run created a root session")

    def prepare(**kwargs: object) -> dict[str, object]:
        parents.append(kwargs.get("parent_session_id"))
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        return _prepared(run_dir)

    status = run_bootstrap(
        project_dir=task,
        spec=_spec(),
        runs_root=task / ".aa" / "runs",
        change_id="BOOT-managed",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=1),
        stop_opencode=lambda handle: None,
        prepare_composition=prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda url, timeout: None,
        shared_endpoint="http://127.0.0.1:4101",
        ensure_run_root=forbidden_root,
        task_directory=task,
        dispose_instance=record_dispose,
    )
    assert status.phase == "terminal"
    assert status.exit_code == 0
    assert status.root_session_id is None
    assert parents == [None]
    plugin = task / ".opencode" / "plugins" / "assurance-boundary.mjs"
    assert plugin.is_file()
    assert "stagedPhysical" in plugin.read_text(encoding="utf-8")
    config = json.loads((task / "opencode.json").read_text(encoding="utf-8"))
    assert "./.opencode/plugins/assurance-boundary.mjs" in config["plugin"]
    assert len(disposed) == 1
    assert disposed[0]["endpoint"] == "http://127.0.0.1:4101"
    assert disposed[0]["directory"] == str(task.resolve())
    assert isinstance(disposed[0]["authorization"], str)
    assert str(disposed[0]["authorization"]).startswith("Basic ")


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
    assert parents == [None]


def test_existing_task_directory_is_reused_for_two_changes(tmp_path: Path, monkeypatch) -> None:
    task = _sut(tmp_path)
    seen: list[Path] = []

    def forbidden(*args, **kwargs):
        raise AssertionError("nested worktree creation")

    monkeypatch.setattr("assurance_product.bootstrap.driver.ensure_run_worktree", forbidden)

    reused: list[object] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        project_dir = kwargs["project_dir"]
        assert isinstance(project_dir, Path)
        seen.append(project_dir)
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        return _prepared(run_dir)

    def start_invocation(**kwargs: object) -> dict[str, object]:
        reused.append(kwargs.get("reuse_directory"))
        return {"invocation_id": kwargs["invocation_id"]}

    for change_id in ("BOOT-A", "BOOT-B"):
        status = run_bootstrap(
            project_dir=tmp_path / "primary",
            spec=_spec(),
            runs_root=task / ".aa" / "runs",
            change_id=change_id,
            environ=_environ(),
            start_opencode_serve=lambda **kwargs: OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=1),
            stop_opencode=lambda handle: None,
            prepare_composition=prepare,
            start_invocation=start_invocation,
            run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
            read_status=lambda **kwargs: {"status": "completed"},
            wait_ready=lambda url, timeout, authorization=None: None,
            shared_endpoint="http://127.0.0.1:4101",
            task_directory=task,
        )
        assert status.exit_code == 0
    assert seen == [task.resolve(), task.resolve()]
    assert reused == [True, True]


def test_reused_task_directory_creates_qa_without_another_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product.cli import _start_invocation

    task = tmp_path / "task"
    task.mkdir()
    subprocess.run(["git", "init", str(task)], check=True, capture_output=True)
    calls: list[Path] = []

    def forbidden(project_dir: Path, change_id: str) -> Path:
        del change_id
        calls.append(project_dir)
        raise AssertionError("nested worktree creation")

    monkeypatch.setattr("assurance_product.cli.ensure_run_worktree", forbidden)
    monkeypatch.setattr(
        "assurance_product.cli._resolve_and_audit",
        lambda **kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        "assurance_product.cli._authorize_secrets",
        lambda composition, secrets: object(),
    )

    class _Application:
        def start(self, **kwargs: object) -> dict[str, object]:
            calls.append(Path(str(kwargs["project_dir"])))
            return {"invocation_id": kwargs["invocation_id"]}

    monkeypatch.setattr("assurance_product.cli.AssuranceProductApplication", _Application)
    _start_invocation(
        project_dir=task,
        change_id="BOOT-qa",
        invocation_id="BOOT-qa",
        product="assurance-opencode",
        binding_dist="bindings",
        binding_entrypoint="deployment",
        binding_declaration="declaration.json",
        config_tree="config",
        entrypoint="full",
        input_path=tmp_path / "input.json",
        secrets=("opencode.token=env:AA_NEXT_OPENCODE_TOKEN",),
        reuse_directory=True,
    )
    assert calls == [task.resolve()]
    assert (task / "qa").is_dir()
    assert not (task / "qa").is_symlink()


def test_managed_run_does_not_create_an_empty_root_session(tmp_path: Path) -> None:
    task = _sut(tmp_path)
    parents: list[object] = []

    def unexpected_root(**_kwargs: object) -> str:
        raise AssertionError("managed Run created an empty root session")

    def prepare(**kwargs: object) -> dict[str, object]:
        parents.append(kwargs.get("parent_session_id"))
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        return _prepared(run_dir)

    status = run_bootstrap(
        project_dir=tmp_path / "primary",
        spec=_spec(),
        runs_root=task / ".aa" / "runs",
        change_id="BOOT-managed",
        environ=_environ(),
        start_opencode_serve=lambda **kwargs: OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=1),
        stop_opencode=lambda handle: None,
        prepare_composition=prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=lambda **kwargs: ({"status": "succeeded"}, "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda url, timeout, authorization=None: None,
        shared_endpoint="http://127.0.0.1:4101",
        ensure_run_root=unexpected_root,
        task_directory=task,
    )
    assert status.root_session_id is None
    assert parents == [None]


def test_missing_task_directory_fails_before_worktree_creation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "assurance_product.bootstrap.driver.ensure_run_worktree",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nested worktree creation")),
    )
    with pytest.raises(ValueError, match="task directory does not exist"):
        run_bootstrap(
            project_dir=tmp_path,
            spec=_spec(),
            runs_root=tmp_path / "runs",
            change_id="BOOT-missing",
            environ=_environ(),
            task_directory=tmp_path / "missing",
        )
