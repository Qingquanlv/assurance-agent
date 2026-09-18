from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from assurance_product.bootstrap.opencode import (
    allocate_loopback_port,
    build_opencode_env,
    start_opencode_serve,
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
