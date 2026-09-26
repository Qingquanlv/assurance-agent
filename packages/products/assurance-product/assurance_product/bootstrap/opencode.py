from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from assurance_product.bootstrap.contracts import OpenCodeHandleV1, RunSpecV1
from assurance_product.opencode_agents import install_opencode_agents

_AMBIENT_OVERRIDES = frozenset(
    {
        "OPENCODE_ENDPOINT",
        "OPENCODE_MODEL",
        "AA_MODEL",
        "PROVIDER_MODEL",
    }
)
_READY_TIMEOUT_SECONDS = 30.0
_STOP_WAIT_SECONDS = 5.0


class OpenCodeLaunchError(Exception):
    pass


def allocate_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    if not isinstance(port, int):
        raise OpenCodeLaunchError("could not allocate a loopback port")
    return port


def build_opencode_env(
    *,
    spec: RunSpecV1,
    run_dir: Path,
    environ: Mapping[str, str],
) -> dict[str, str]:
    env = {key: value for key, value in environ.items() if key not in _AMBIENT_OVERRIDES}
    env.update(spec.sut.env)
    for name in spec.sut.env_from_node:
        if name in environ:
            env[name] = environ[name]
    env["XDG_CONFIG_HOME"] = str((run_dir / "opencode-config").resolve())
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    token = env.get(spec.opencode_token_env)
    if token:
        env["OPENCODE_SERVER_PASSWORD"] = token
    return env


def _basic_opencode_authorization(token: str) -> str:
    encoded = base64.b64encode(f"opencode:{token}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def dispose_project_instance(
    *,
    endpoint: str,
    directory: str,
    authorization: str | None = None,
) -> None:
    """Drop a cached OpenCode project instance so the next session reloads plugins."""
    query = urlencode({"directory": directory})
    url = f"{endpoint.rstrip('/')}/instance/dispose?{query}"
    request = Request(url, data=b"", method="POST")
    if authorization:
        request.add_header("Authorization", authorization)
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - local OpenCode control call
            status = int(response.status)
    except HTTPError as error:
        raise OpenCodeLaunchError(f"instance dispose failed: HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise OpenCodeLaunchError(f"instance dispose failed: {error}") from error
    if not 200 <= status < 300:
        raise OpenCodeLaunchError(f"instance dispose failed: HTTP {status}")


def wait_http_ready(
    url: str,
    *,
    timeout: float,
    authorization: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET")
            if authorization:
                request.add_header("Authorization", authorization)
            with urlopen(request, timeout=2) as response:  # noqa: S310 - local readiness probe
                if 200 <= int(response.status) < 300:
                    return
                last_error = OSError(f"unexpected status {response.status}")
        except HTTPError as error:
            if authorization is None and 400 <= error.code < 500:
                return
            last_error = error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
        time.sleep(0.1)
    raise OpenCodeLaunchError(f"endpoint did not become ready at {url}: {last_error}")


def start_opencode_serve(
    *,
    spec: RunSpecV1,
    project_dir: Path,
    run_dir: Path,
    environ: Mapping[str, str],
    spawn: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    wait: Callable[[str, float], None] | None = None,
) -> OpenCodeHandleV1:
    try:
        install_opencode_agents(project_dir)
    except ValueError as error:
        raise OpenCodeLaunchError(str(error)) from error
    port = allocate_loopback_port()
    endpoint = f"http://127.0.0.1:{port}"
    resolver = which or shutil.which
    binary = resolver("opencode")
    if not binary:
        raise OpenCodeLaunchError("opencode is not on PATH")
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "opencode.log"
    log_path.touch()
    env = build_opencode_env(spec=spec, run_dir=run_dir, environ=environ)
    token = env.get("OPENCODE_SERVER_PASSWORD")
    authorization = _basic_opencode_authorization(token) if token else None
    command = [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    launcher = spawn or subprocess.Popen
    with log_path.open("ab", buffering=0) as log:
        process = launcher(
            command,
            cwd=project_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    waiter = wait or (lambda url, timeout: wait_http_ready(url, timeout=timeout, authorization=authorization))
    try:
        waiter(f"{endpoint}/global/health", _READY_TIMEOUT_SECONDS)
    except OpenCodeLaunchError:
        waiter(f"{endpoint}/", _READY_TIMEOUT_SECONDS)
    return OpenCodeHandleV1(endpoint=endpoint, pid=int(process.pid))


def attach_shared_opencode(
    *,
    endpoint: str,
    authorization: str | None = None,
    probe: Callable[..., None] | None = None,
) -> OpenCodeHandleV1:
    """Borrow an already running loopback OpenCode server. The handle has no PID."""
    ready = probe or wait_http_ready
    health = endpoint.rstrip("/") + "/global/health"
    try:
        ready(health, timeout=5, authorization=authorization)
    except TypeError:
        ready(health, timeout=5)
    return OpenCodeHandleV1(endpoint=endpoint, ownership="shared")


def create_run_root_session(
    *,
    endpoint: str,
    directory: str,
    change_id: str,
    origin_session_id: str | None,
    opener: Callable[..., object],
) -> str:
    """Create one unprompted session for the run. Children use its id as parentID."""
    created = opener(
        "POST",
        "/session",
        {
            "title": f"aa:{change_id}",
            "metadata": {
                "role": "run_root",
                "change_id": change_id,
                "origin_session_id": origin_session_id,
            },
        },
        directory,
    )
    if not isinstance(created, Mapping) or not isinstance(created.get("id"), str) or not created["id"]:
        raise OpenCodeLaunchError("run root session id is missing")
    if created.get("parentID"):
        raise OpenCodeLaunchError("run root must not have a parent")
    messages = opener("GET", f"/session/{created['id']}/message", None, directory)
    if messages:
        raise OpenCodeLaunchError("run root must not contain messages")
    return str(created["id"])


def stop_opencode(handle: OpenCodeHandleV1) -> None:
    if handle.ownership == "shared" or handle.pid is None:
        return
    try:
        os.killpg(handle.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(handle.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
    deadline = time.monotonic() + _STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(handle.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            time.sleep(0.1)
            continue
        time.sleep(0.1)
    try:
        os.killpg(handle.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(handle.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            return
