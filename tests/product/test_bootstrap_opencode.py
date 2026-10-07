from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from assurance_product.bootstrap.contracts import OpenCodeHandleV1
from assurance_product.bootstrap.opencode import (
    OpenCodeLaunchError,
    allocate_loopback_port,
    attach_shared_opencode,
    build_opencode_env,
    create_run_root_session,
    start_opencode_serve,
    stop_opencode,
)
from tests.product.test_bootstrap_contracts import _spec


def test_allocate_loopback_port_is_unique() -> None:
    first = allocate_loopback_port()
    second = allocate_loopback_port()
    assert first != second
    assert 1024 <= first <= 65535


def test_build_opencode_env_strips_overrides_and_sets_xdg(tmp_path: Path) -> None:
    env = build_opencode_env(
        spec=_spec(),
        run_dir=tmp_path,
        environ={
            "PATH": "/bin",
            "OPENCODE_MODEL": "should-drop",
            "QA_ADMIN_PASSWORD": "secret",
            "AA_NEXT_OPENCODE_TOKEN": "tok",
            "HOME": "/tmp",
        },
    )
    assert "OPENCODE_MODEL" not in env
    assert env["BASE_URL"] == "http://127.0.0.1:9999"
    assert env["QA_ADMIN_PASSWORD"] == "secret"
    assert env["OPENCODE_SERVER_PASSWORD"] == "tok"
    assert env["XDG_CONFIG_HOME"] == str((tmp_path / "opencode-config").resolve())


def test_start_opencode_serve_uses_injected_spawn(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "sut"
    project.mkdir()
    monkeypatch.setattr(
        "assurance_product.bootstrap.opencode.install_opencode_agents",
        lambda root: (
            root / "opencode.json",
            root / ".opencode" / "plugins" / "assurance-boundary.mjs",
        ),
    )
    spawned: list[tuple[list[str], dict[str, object]]] = []

    def fake_spawn(args: list[str], **kwargs: object) -> SimpleNamespace:
        spawned.append((args, kwargs))
        return SimpleNamespace(pid=4242)

    handle = start_opencode_serve(
        spec=_spec(),
        project_dir=project,
        run_dir=tmp_path / "run",
        environ={"PATH": "/bin", "QA_ADMIN_PASSWORD": "x", "AA_NEXT_OPENCODE_TOKEN": "t"},
        spawn=fake_spawn,
        which=lambda name: "/usr/bin/opencode" if name == "opencode" else None,
        wait=lambda url, timeout: None,
    )
    assert handle.pid == 4242
    assert handle.endpoint.startswith("http://127.0.0.1:")
    args, kwargs = spawned[0]
    assert args[:3] == ["/usr/bin/opencode", "serve", "--hostname"]
    assert kwargs["cwd"] == project


def test_attach_shared_opencode_probes_health_and_does_not_install_agents(monkeypatch) -> None:
    probed: list[str] = []

    def probe(url: str, timeout: float, authorization: str | None = None) -> None:
        del timeout
        assert authorization == "Basic dG9rZW4="
        probed.append(url)

    monkeypatch.setattr(
        "assurance_product.bootstrap.opencode.install_opencode_agents",
        lambda root: (_ for _ in ()).throw(AssertionError("install")),
    )
    handle = attach_shared_opencode(
        endpoint="http://127.0.0.1:4096",
        authorization="Basic dG9rZW4=",
        probe=probe,
    )
    assert handle.ownership == "shared"
    assert handle.pid is None
    assert probed == ["http://127.0.0.1:4096/global/health"]


def test_attach_shared_opencode_reports_an_unreachable_endpoint() -> None:
    def probe(url: str, timeout: float, authorization: str | None = None) -> None:
        del url, timeout, authorization
        raise OpenCodeLaunchError("connection refused")

    try:
        attach_shared_opencode(endpoint="http://127.0.0.1:4096", probe=probe)
    except OpenCodeLaunchError as error:
        assert "connection refused" in str(error)
    else:
        raise AssertionError("unreachable shared server was treated as attached")


def test_stop_opencode_does_not_signal_a_shared_handle(monkeypatch) -> None:
    def fail_kill(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("shared server was signaled")

    monkeypatch.setattr("assurance_product.worker_lifecycle.os.kill", fail_kill)
    monkeypatch.setattr("assurance_product.worker_lifecycle.os.killpg", fail_kill)
    stop_opencode(OpenCodeHandleV1(endpoint="http://127.0.0.1:4096", ownership="shared"))


def test_create_run_root_session_is_unprompted_and_has_no_parent() -> None:
    calls: list[tuple[str, str]] = []

    def opener(method: str, path: str, body: object, directory: str) -> object:
        del directory
        calls.append((method, path))
        if method == "POST":
            assert isinstance(body, dict)
            assert body["title"] == "aa:BOOT-1"
            assert "parentID" not in body
            return {"id": "ses_root"}
        return []

    session_id = create_run_root_session(
        endpoint="http://127.0.0.1:4096",
        directory="/work",
        change_id="BOOT-1",
        origin_session_id="ses_user",
        opener=opener,
    )
    assert session_id == "ses_root"
    assert calls == [("POST", "/session"), ("GET", "/session/ses_root/message")]


def test_create_run_root_session_rejects_a_prompted_or_child_session() -> None:
    def child(method: str, path: str, body: object, directory: str) -> object:
        del method, path, body, directory
        return {"id": "ses_root", "parentID": "ses_user"}

    try:
        create_run_root_session(
            endpoint="http://127.0.0.1:4096",
            directory="/work",
            change_id="BOOT-1",
            origin_session_id=None,
            opener=child,
        )
    except OpenCodeLaunchError as error:
        assert "parent" in str(error)
    else:
        raise AssertionError("child root was accepted")


def test_private_server_without_native_identity_is_not_signaled(monkeypatch) -> None:
    import pytest
    from assurance_product.bootstrap.opencode import OpenCodeLaunchError, stop_opencode

    calls = []
    monkeypatch.setattr("os.killpg", lambda *args: calls.append(args))
    with pytest.raises(OpenCodeLaunchError, match="identity"):
        stop_opencode(OpenCodeHandleV1(endpoint="http://127.0.0.1:4096", pid=1234))
    assert calls == []
