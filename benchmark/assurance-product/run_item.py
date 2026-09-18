"""Fail-closed Phase 5 provider-live driver through aa-next."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from fnmatch import fnmatchcase
import hashlib
from importlib.resources import files
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

_AMBIENT_OVERRIDE_VARS = frozenset(
    {
        "OPENCODE_ENDPOINT",
        "OPENCODE_MODEL",
        "AA_MODEL",
        "PROVIDER_MODEL",
    }
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[:=](?!=)\s*\S+|sk-[A-Za-z0-9-]+"
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "interrupted"})
_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS = 300
_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS = 10
_OPENCODE_RESOLVED_READ_ATTEMPTS = 3
_BACKEND_URL = "http://127.0.0.1:9999"
_FRONTEND_URL = "http://127.0.0.1:3100"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fail(message: str) -> int:
    print(f"phase5-live: {message}", file=sys.stderr)
    return 1


def _reject_ambient_overrides() -> None:
    for name in _AMBIENT_OVERRIDE_VARS:
        if os.environ.get(name):
            raise SystemExit(f"ambient override {name!r} is forbidden")


def _reject_credentials_in_text(text: str, *, label: str) -> None:
    if _CREDENTIAL_PATTERN.search(text):
        raise SystemExit(f"credential material detected in {label}")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _reject_credentials_in_text(text, label=str(path))
    path.write_text(text, encoding="utf-8")


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.name != "manifest.json":
        raise SystemExit("driver must consume the committed manifest.json only")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("manifest must be a JSON object")
    return document


def _manifest_item(document: Mapping[str, Any], item_id: str, adapter: str) -> dict[str, Any]:
    items = document.get("items")
    if not isinstance(items, list):
        raise SystemExit("manifest must declare items[]")
    matches = [item for item in items if isinstance(item, dict) and item.get("id") == item_id]
    if len(matches) != 1:
        raise SystemExit(f"manifest must declare exactly one item {item_id!r}")
    item = matches[0]
    if adapter != "opencode":
        raise SystemExit(f"unsupported adapter {adapter!r}")
    product = "assurance-opencode"
    if item.get("product") != product:
        raise SystemExit(f"adapter/product mismatch: {adapter} != {item.get('product')}")
    if item.get("adapter_version") != "0.1.0":
        raise SystemExit(f"unsupported adapter_version {item.get('adapter_version')!r}")
    routes = item.get("routing_assignments")
    binding_routes = item.get("deployment_binding_routes")
    if not isinstance(routes, dict) or routes != binding_routes:
        raise SystemExit("routing_assignments must equal deployment_binding_routes")
    models = {assignment.get("provider_model") for assignment in routes.values()}
    workers = {assignment.get("worker_profile") for assignment in routes.values()}
    if models != {"deepseek/deepseek-v4-flash"}:
        raise SystemExit(f"OpenCode model mismatch: {sorted(models)}")
    if workers != {"max"}:
        raise SystemExit(f"OpenCode worker mismatch: {sorted(workers)}")
    families = tuple(item.get("selected_test_families") or ())
    canonical_families = ("api", "e2e", "fuzz", "performance")
    if item.get("entrypoint") == "full" and (
        not families or families != tuple(family for family in canonical_families if family in families)
    ):
        raise SystemExit("full entrypoint requires a non-empty canonical family selection")
    raw_case_modules = item.get("case_modules")
    if not isinstance(raw_case_modules, list) or any(
        not isinstance(module, str) for module in raw_case_modules
    ):
        raise SystemExit("case_modules must be a list of strings")
    case_modules = tuple(module for module in raw_case_modules if isinstance(module, str))
    if not case_modules or case_modules != tuple(sorted(set(case_modules))):
        raise SystemExit("case_modules must be a non-empty sorted unique list")
    for module in case_modules:
        if not isinstance(module, str) or any(
            not part or part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
            for part in module.split("/")
        ):
            raise SystemExit(f"case_modules contains an unsafe module path: {module!r}")
    return item


def _resolve_sut(repo: Path, relative: str) -> Path:
    candidates = (
        repo / relative,
        repo.parents[1] / relative,
    )
    for candidate in candidates:
        if (candidate / "app").is_dir() and (candidate / "web").is_dir():
            return candidate.resolve()
    raise SystemExit(f"live SUT is missing at {relative}")


def _sut_worktree_home(repo: Path) -> Path:
    return repo / ".worktrees"


def _sut_git_root(sut_root: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(sut_root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"SUT is not a git repository: {sut_root}")
    root = Path(completed.stdout.strip()).resolve()
    if root != sut_root.resolve():
        raise SystemExit(
            f"SUT git root is {root}, not {sut_root.resolve()}; refuse to worktree the parent repo"
        )
    return root


def _seed_worktree_runtime(source: Path, dest: Path) -> None:
    from assurance_product.opencode_agents import _opencode_config

    config = dest / "opencode.json"
    config.write_text(_opencode_config(), encoding="utf-8")
    plugin = dest / ".opencode" / "plugins" / "assurance-boundary.mjs"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_bytes(
        files("assurance_product").joinpath("resources", "opencode", "assurance-boundary.mjs").read_bytes()
    )
    source_migrations = source / "migrations"
    dest_migrations = dest / "migrations"
    if source_migrations.is_dir() and not dest_migrations.exists():
        shutil.copytree(source_migrations, dest_migrations)


def _primary_worktree_root(project_dir: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(project_dir), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :]).resolve()
    return None


def _frontend_web_root(project_dir: Path) -> Path:
    candidates = [project_dir / "web"]
    primary = _primary_worktree_root(project_dir)
    if primary is not None:
        primary_web = primary / "web"
        if primary_web.resolve() != candidates[0].resolve():
            candidates.append(primary_web)
    for web in candidates:
        vite = web / "node_modules" / ".bin" / "vite"
        lockfile = web / "pnpm-lock.yaml"
        if vite.is_file() and os.access(vite, os.X_OK) and lockfile.is_file():
            return web.resolve()
    raise SystemExit("managed SUT frontend requires pinned pnpm dependencies and executable Vite")


def _prepare_sut_worktree(*, repo: Path, sut_root: Path, change_id: str) -> Path:
    source = _sut_git_root(sut_root)
    dest = (_sut_worktree_home(repo) / source.name / change_id).resolve()
    if dest == source or source in dest.parents:
        raise SystemExit(f"worktree destination must be outside the SUT checkout: {dest}")
    if dest.exists():
        raise SystemExit(f"worktree destination must be fresh: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    added = subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-b", f"bench/{change_id}", str(dest)],
        check=False,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        raise SystemExit(f"SUT worktree add failed: {added.stderr.strip() or added.stdout.strip()}")
    _seed_worktree_runtime(source, dest)
    return dest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _acg_plan(change_root: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    plans = sorted(change_root.glob("results/plan/*/resolved-assurance-plan.json"))
    if len(plans) != 1 or not plans[0].is_file() or plans[0].is_symlink():
        return None, None
    data = plans[0].read_bytes()
    try:
        plan = json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(plan, dict):
        return None, None
    relative = plans[0].relative_to(change_root)
    return plan, {
        "path": f"qa/{relative.as_posix()}",
        "digest": _sha256(data),
    }


def _required_runtime_environment(output: Path) -> dict[str, str]:
    sqlite_file = output / "sut-runtime" / "db.sqlite3"
    return {
        "AA_ADMIN_PASSWORD": "123456",
        "AA_ADMIN_USERNAME": "admin",
        "AA_BASE_URL": _BACKEND_URL,
        "AA_SQLITE_PATH": str(sqlite_file),
        "API_BASE_URL": _BACKEND_URL,
        "BASE_URL": _BACKEND_URL,
        "E2E_BACKEND_URL": _BACKEND_URL,
        "E2E_FRONTEND_URL": _FRONTEND_URL,
        "FRONTEND_URL": _FRONTEND_URL,
        "FUZZ_SCHEMA_MODE": "uri",
        "NO_PROXY": "127.0.0.1,localhost",
        "QA_ADMIN_PASSWORD": "123456",
        "QA_ADMIN_USERNAME": "admin",
        "QA_FUZZ_SCHEMA_MODE": "uri",
        "QA_SQLITE_FILE": str(sqlite_file),
        "no_proxy": "127.0.0.1,localhost",
    }


def _required_opencode_environment(output: Path) -> dict[str, str]:
    runtime = _required_runtime_environment(output)
    # Load only the product's project config/plugins; keep the user's OAuth data
    # directory unchanged and out of the benchmark's artifacts.
    runtime["XDG_CONFIG_HOME"] = str(output.parent / ".opencode-config" / output.name)
    for name in (
        "AA_ADMIN_PASSWORD",
        "AA_ADMIN_USERNAME",
        "QA_ADMIN_PASSWORD",
        "QA_ADMIN_USERNAME",
    ):
        runtime.pop(name)
    return runtime


def _runtime_environment_errors(environment: Mapping[str, str], *, output: Path) -> list[str]:
    return [
        f"OpenCode server environment {name} must equal {expected!r}"
        for name, expected in _required_opencode_environment(output).items()
        if environment.get(name) != expected
    ]


def derive_change_id(*, item_id: str, stamp: str, nonce: str) -> str:
    parts = (("item", item_id), ("stamp", stamp), ("nonce", nonce))
    for label, value in parts:
        if not value or any(character in value for character in ("/", "\\", " ", "\x00")):
            raise SystemExit(f"{label} is not a safe change-id component")
    return f"BENCH-{item_id}-{stamp}-{nonce}"


def _write_bootstrap_spec(
    path: Path,
    *,
    item: Mapping[str, Any],
    requirement: str,
    timeout_seconds: int,
) -> None:
    routes = item["routing_assignments"]
    if not isinstance(routes, Mapping) or not routes:
        raise SystemExit("manifest item is missing routing_assignments")
    first = next(iter(routes.values()))
    if not isinstance(first, Mapping):
        raise SystemExit("routing assignment must be a mapping")
    document = {
        "schema_version": "1",
        "product": "assurance-opencode",
        "entrypoint": item["entrypoint"],
        "requirement": requirement,
        "candidate_test_families": list(item["selected_test_families"]),
        "case_modules": list(item["case_modules"]),
        "sut": {
            "base_url": _BACKEND_URL,
            "readiness_url": f"{_BACKEND_URL}/openapi.json",
            "env": {"BASE_URL": _BACKEND_URL},
            "env_from_node": ["QA_ADMIN_PASSWORD"],
        },
        "routes": {
            "provider_model": first["provider_model"],
            "worker_profile": first["worker_profile"],
        },
        "budgets": {
            "review_rounds": 4,
            "coverage_rounds": 2,
            "healing_rounds": 2,
            "execution_retries": 2,
        },
        "opencode_token_env": item["secret_env"],
        "timeout_seconds": timeout_seconds,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    _reject_credentials_in_text(text, label=str(path))
    path.write_text(text, encoding="utf-8")


_PRODUCT_WHEEL_PACKAGES = (
    "graph-engine",
    "agent-runtime-contracts",
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
    "assurance-product",
    "agent-runtime-opencode",
)


def _aa_next(
    binary: Path,
    *args: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(binary), *args],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    label: str,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed


class _SutRuntime:
    __slots__ = (
        "backend_log",
        "backend_url",
        "env",
        "frontend_log",
        "frontend_url",
        "sqlite_file",
    )

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        backend_url: str,
        frontend_url: str,
        sqlite_file: Path,
        backend_log: Path,
        frontend_log: Path,
    ) -> None:
        self.env = env
        self.backend_url = backend_url
        self.frontend_url = frontend_url
        self.sqlite_file = sqlite_file
        self.backend_log = backend_log
        self.frontend_log = frontend_log


def _prepare_sut_python(
    *,
    project_dir: Path,
    runtime_root: Path,
    env: Mapping[str, str],
) -> Path:
    for required in (project_dir / "pyproject.toml", project_dir / "uv.lock"):
        if not required.is_file() or required.is_symlink():
            raise SystemExit(f"managed SUT dependency input is missing or unsafe: {required}")
    venv = runtime_root / "venv"
    _run_checked(
        ["uv", "venv", "--python", "3.11", str(venv)],
        cwd=project_dir,
        env=env,
        timeout=120,
        label="managed SUT uv venv",
    )
    active_env = dict(env)
    active_env["VIRTUAL_ENV"] = str(venv)
    _run_checked(
        [
            "uv",
            "sync",
            "--active",
            "--frozen",
            "--project",
            str(project_dir),
            "--no-install-project",
        ],
        cwd=project_dir,
        env=active_env,
        timeout=900,
        label="managed SUT frozen dependency sync",
    )
    python = venv / "bin" / "python"
    if not python.is_file():
        raise SystemExit("managed SUT environment is missing Python")
    return python


def _copy_runtime_component(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise SystemExit(f"managed SUT runtime source is missing or unsafe: {source}")
    symbolic_links = [path for path in source.rglob("*") if path.is_symlink()]
    if symbolic_links:
        raise SystemExit(f"managed SUT runtime source contains symbolic links: {symbolic_links[0]}")
    shutil.copytree(source, destination)


def _assert_loopback_port_available(port: int) -> None:
    try:
        with socket.socket() as probe:
            probe.settimeout(0.25)
            occupied = probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError as error:
        raise SystemExit(f"managed SUT loopback port could not be checked: {port}") from error
    if occupied:
        raise SystemExit(f"managed SUT loopback port is already in use: {port}")


def _spawn_managed_process(
    *,
    label: str,
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log:
        try:
            return subprocess.Popen(  # noqa: S603
                list(command),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            raise SystemExit(f"managed SUT {label} could not start: {error}") from error


def _stop_managed_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    expected_group = process.pid
    try:
        actual_group = os.getpgid(process.pid)
    except ProcessLookupError:
        process.wait(timeout=1)
        return
    if actual_group != expected_group:
        raise SystemExit(
            f"refusing to terminate managed SUT process with changed group identity: "
            f"pid={process.pid} expected={expected_group} actual={actual_group}"
        )
    os.killpg(expected_group, signal.SIGTERM)
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is not None:
        return
    try:
        actual_group = os.getpgid(process.pid)
    except ProcessLookupError:
        process.wait(timeout=1)
        return
    if actual_group != expected_group:
        raise SystemExit("refusing to kill a managed SUT process after group identity changed")
    os.killpg(expected_group, signal.SIGKILL)
    process.wait(timeout=5)


def _wait_http_ready(
    url: str,
    *,
    accept: str,
    label: str,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: object = "not attempted"
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise SystemExit(f"managed SUT {label} exited before readiness: {returncode}")
        try:
            request = Request(url, headers={"Accept": accept}, method="GET")
            with urlopen(request, timeout=2) as response:  # noqa: S310 - pinned loopback URL
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, URLError, TimeoutError) as error:
            last_error = error
        time.sleep(0.25)
    raise SystemExit(f"managed SUT {label} failed readiness at {url}: {last_error}")


def _acquire_token(
    backend_url: str,
    *,
    username: str,
    password: str,
) -> str:
    request = Request(
        f"{backend_url}/api/v1/base/access_token",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - pinned loopback URL
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"managed SUT login failed for {username!r}: {error}") from error
    token = body.get("data", {}).get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise SystemExit(f"managed SUT login for {username!r} returned no data.access_token")
    return token


def _request_sut_json(
    backend_url: str,
    path: str,
    *,
    method: str,
    token: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(dict(payload)).encode() if payload is not None else None
    request = Request(
        f"{backend_url}{path}",
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "token": token,
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - pinned loopback URL
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"managed SUT provisioning request {method} {path} failed: {error}") from error
    if not isinstance(body, dict) or body.get("code") != 200:
        raise SystemExit(f"managed SUT provisioning request {method} {path} was rejected")
    return body


def _provision_limited_role_identity(
    backend_url: str,
    *,
    admin_token: str,
) -> tuple[str, str, str]:
    role_name = "aa-limited"
    username = "aa_limited"
    password = secrets.token_urlsafe(24)
    _request_sut_json(
        backend_url,
        "/api/v1/role/create",
        method="POST",
        token=admin_token,
        payload={"name": role_name, "desc": "Assurance benchmark restricted role"},
    )
    roles = _request_sut_json(
        backend_url,
        "/api/v1/role/list?" + urlencode({"page": 1, "page_size": 100, "role_name": role_name}),
        method="GET",
        token=admin_token,
    ).get("data")
    matches = [role for role in roles or [] if isinstance(role, dict) and role.get("name") == role_name]
    if len(matches) != 1:
        raise SystemExit("managed SUT limited role provisioning did not resolve exactly one role")
    role_id = matches[0].get("id")
    if isinstance(role_id, bool) or not isinstance(role_id, int):
        raise SystemExit("managed SUT limited role provisioning returned an invalid role id")
    _request_sut_json(
        backend_url,
        "/api/v1/role/authorized",
        method="POST",
        token=admin_token,
        payload={"id": role_id, "menu_ids": [], "api_infos": []},
    )
    _request_sut_json(
        backend_url,
        "/api/v1/user/create",
        method="POST",
        token=admin_token,
        payload={
            "email": "aa_limited@example.com",
            "username": username,
            "password": password,
            "is_active": True,
            "is_superuser": False,
            "role_ids": [role_id],
            "dept_id": 0,
        },
    )
    return _acquire_token(backend_url, username=username, password=password), username, password


@contextmanager
def _managed_sut_runtime(
    *,
    repo: Path,
    project_dir: Path,
    output: Path,
    env: Mapping[str, str],
) -> Iterator[_SutRuntime]:
    _assert_loopback_port_available(9999)
    _assert_loopback_port_available(3100)
    runtime_root = output / "sut-runtime"
    if runtime_root.exists():
        raise SystemExit(f"managed SUT runtime directory must be fresh: {runtime_root}")
    runtime_root.mkdir(parents=True)
    python = _prepare_sut_python(project_dir=project_dir, runtime_root=runtime_root, env=env)
    _copy_runtime_component(project_dir / "app", runtime_root / "app")
    _copy_runtime_component(project_dir / "migrations", runtime_root / "migrations")
    sqlite_file = runtime_root / "db.sqlite3"
    backend_log = output / "sut-backend.log"
    frontend_log = output / "sut-frontend.log"
    runtime_env = dict(env)
    runtime_env.update(_required_runtime_environment(output))
    web = _frontend_web_root(project_dir)
    vite = web / "node_modules" / ".bin" / "vite"
    backend: subprocess.Popen[bytes] | None = None
    frontend: subprocess.Popen[bytes] | None = None
    body_failed = False
    try:
        backend = _spawn_managed_process(
            label="backend",
            command=(
                str(python),
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "9999",
            ),
            cwd=runtime_root,
            env=runtime_env,
            log_path=backend_log,
        )
        _wait_http_ready(
            f"{_BACKEND_URL}/openapi.json",
            accept="application/json",
            label="backend",
            process=backend,
            timeout=90,
        )
        token = _acquire_token(_BACKEND_URL, username="admin", password="123456")
        runtime_env["API_ADMIN_TOKEN"] = token
        runtime_env["E2E_API_TOKEN"] = token
        limited_token, limited_username, limited_password = _provision_limited_role_identity(
            _BACKEND_URL,
            admin_token=token,
        )
        runtime_env["API_LIMITED_ROLE_USER_TOKEN"] = limited_token
        runtime_env["QA_LIMITED_USERNAME"] = limited_username
        runtime_env["QA_LIMITED_PASSWORD"] = limited_password
        frontend = _spawn_managed_process(
            label="frontend",
            command=(str(vite), "--host", "127.0.0.1", "--port", "3100", "--strictPort", "--no-open"),
            cwd=web,
            env=runtime_env,
            log_path=frontend_log,
        )
        _wait_http_ready(
            _FRONTEND_URL,
            accept="text/html,application/xhtml+xml",
            label="frontend",
            process=frontend,
            timeout=90,
        )
        yield _SutRuntime(
            env=runtime_env,
            backend_url=_BACKEND_URL,
            frontend_url=_FRONTEND_URL,
            sqlite_file=sqlite_file,
            backend_log=backend_log,
            frontend_log=frontend_log,
        )
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_errors: list[str] = []
        for process in (frontend, backend):
            if process is None:
                continue
            try:
                _stop_managed_process(process)
            except (OSError, subprocess.SubprocessError, SystemExit) as error:
                cleanup_errors.append(str(error))
        if cleanup_errors and not body_failed:
            raise SystemExit("managed SUT cleanup failed: " + "; ".join(cleanup_errors))


def _prepare_installed_product_env(
    *,
    repo: Path,
    output: Path,
    env: Mapping[str, str],
    adapter: str,
) -> tuple[Path, Path]:
    dist_root = output / "wheels"
    dist_root.mkdir()
    venv = (output / "venv").resolve()
    for package in _PRODUCT_WHEEL_PACKAGES:
        _run_checked(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist_root),
                "--package",
                package,
            ],
            cwd=repo,
            env=env,
            timeout=180,
            label=f"uv build {package}",
        )
    _run_checked(
        ["uv", "venv", "--python", "3.11", str(venv)],
        cwd=repo,
        env=env,
        timeout=60,
        label="uv venv",
    )
    python = venv / "bin" / "python"
    aa_next = venv / "bin" / "aa"
    _run_checked(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(dist_root),
            f"assurance-product[{adapter}]",
        ],
        cwd=repo,
        env=env,
        timeout=180,
        label=f"install assurance-product[{adapter}]",
    )
    if not aa_next.is_file():
        raise SystemExit("isolated env is missing aa")
    return python, aa_next


def _parse_json(output: str, *, label: str) -> dict[str, Any]:
    text = output.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} did not emit JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} JSON must be an object")
    return payload


def _last_json_object(path: Path, *, start: int = 0) -> dict[str, Any] | None:
    if start < 0:
        raise ValueError("run log start offset must be non-negative")
    with path.open(encoding="utf-8") as stream:
        stream.seek(start)
        text = stream.read()
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return {
                "invocation_id": payload.get("invocation_id"),
                "status": payload.get("status"),
                "terminal_reason": payload.get("terminal_reason"),
                "actions": payload.get("actions") or [],
            }
    return None


def _load_opencode_secret(secret_env: str) -> None:
    if os.environ.get(secret_env):
        return
    phase3 = os.environ.get("OPENCODE_PHASE3_TOKEN")
    os.environ[secret_env] = phase3 if phase3 is not None else ""


def _check_opencode(endpoint: str) -> int:
    origin = endpoint.rstrip("/")
    try:
        request = Request(f"{origin}/global/health", headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=5) as response:  # noqa: S310 - pinned local endpoint
            health = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as error:
        return _fail(f"OpenCode server at pinned endpoint {origin} is unavailable: {error}")
    if not isinstance(health, dict):
        return _fail("OpenCode health response is not an object")
    return 0


def _agent_profile_errors(
    resolved_config: Mapping[str, Any], expected_config: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    resolved_agents = resolved_config.get("agent")
    expected_agents = expected_config.get("agent")
    if not isinstance(resolved_agents, Mapping):
        return ["resolved OpenCode config has no agent mapping"]
    if not isinstance(expected_agents, Mapping):
        return ["installed OpenCode config has no agent mapping"]
    for profile, expected_value in expected_agents.items():
        if not isinstance(profile, str) or not isinstance(expected_value, Mapping):
            errors.append(f"installed OpenCode agent definition is invalid: {profile!r}")
            continue
        resolved_value = resolved_agents.get(profile)
        if not isinstance(resolved_value, Mapping):
            errors.append(f"{profile}: agent is absent from resolved OpenCode config")
            continue
        expected_prompt = expected_value.get("prompt")
        if resolved_value.get("prompt") != expected_prompt:
            errors.append(f"{profile}: resolved prompt differs from installed project profile")
        expected_permission = expected_value.get("permission")
        resolved_permission = resolved_value.get("permission")
        if not isinstance(expected_permission, Mapping) or not isinstance(resolved_permission, Mapping):
            errors.append(f"{profile}: permission mapping is absent")
            continue
        for permission, expected_rule in expected_permission.items():
            resolved_rule = resolved_permission.get(permission)
            if isinstance(expected_rule, Mapping):
                if not isinstance(resolved_rule, Mapping):
                    errors.append(f"{profile}: permission {permission!s} is not a rule mapping")
                    continue
                for pattern, action in expected_rule.items():
                    actual = resolved_rule.get(pattern)
                    if actual != action:
                        errors.append(
                            f"{profile}: permission {permission!s}[{pattern!r}] resolved to "
                            f"{actual!r}, expected {action!r}"
                        )
                for pattern, action in resolved_rule.items():
                    if action == "allow" and expected_rule.get(pattern) != "allow":
                        expected_allows = (
                            candidate
                            for candidate, candidate_action in expected_rule.items()
                            if candidate_action == "allow" and isinstance(candidate, str)
                        )
                        is_narrower_allow = isinstance(pattern, str) and any(
                            fnmatchcase(pattern, candidate) for candidate in expected_allows
                        )
                        if pattern not in expected_rule and not is_narrower_allow:
                            errors.append(
                                f"{profile}: permission {permission!s}[{pattern!r}] has an ambient allow"
                            )
            elif resolved_rule != expected_rule:
                errors.append(
                    f"{profile}: permission {permission!s} resolved to {resolved_rule!r}, "
                    f"expected {expected_rule!r}"
                )
        expected_tools = expected_value.get("tools")
        resolved_tools = resolved_value.get("tools")
        if not isinstance(expected_tools, Mapping):
            errors.append(f"{profile}: installed tools mapping is absent")
            continue
        for tool, enabled in expected_tools.items():
            if not isinstance(tool, str):
                errors.append(f"{profile}: installed tool name is not text")
                continue
            if tool in expected_permission and isinstance(expected_permission[tool], Mapping):
                continue
            actual: object
            if isinstance(resolved_tools, Mapping) and tool in resolved_tools:
                actual = resolved_tools[tool]
            else:
                action = resolved_permission.get(tool)
                actual = (
                    {"allow": True, "deny": False}.get(action, action) if isinstance(action, str) else action
                )
            if actual is not enabled:
                errors.append(f"{profile}: tool {tool!s} resolved to {actual!r}, expected {enabled!r}")
    return errors


def _product_locked_opencode_config() -> dict[str, Any]:
    from assurance_product.opencode_agents import _opencode_config

    document = json.loads(_opencode_config())
    if not isinstance(document, dict):
        raise SystemExit("product-locked OpenCode config is not an object")
    return document


def _project_opencode_asset_errors(project_dir: Path) -> list[str]:
    from assurance_product.opencode_agents import _opencode_config

    expected = (
        (
            project_dir / "opencode.json",
            _opencode_config().encode("utf-8"),
            "project OpenCode config differs from the installed product",
        ),
        (
            project_dir / ".opencode" / "plugins" / "assurance-boundary.mjs",
            files("assurance_product")
            .joinpath("resources", "opencode", "assurance-boundary.mjs")
            .read_bytes(),
            "project OpenCode boundary plugin differs from the installed product",
        ),
    )
    errors: list[str] = []
    for path, expected_bytes, message in expected:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_bytes:
            errors.append(message)
    return errors


def _check_opencode_agent_profiles(endpoint: str, project_dir: Path) -> list[str]:
    try:
        expected = _product_locked_opencode_config()
    except (SystemExit, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        return [f"could not read product-locked OpenCode agent config: {error}"]
    last_error: Exception | None = None
    for attempt in range(_OPENCODE_RESOLVED_READ_ATTEMPTS):
        try:
            query = urlencode({"directory": str(project_dir.resolve())})
            request = Request(
                f"{endpoint.rstrip('/')}/config?{query}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS) as response:  # noqa: S310 - pinned local endpoint
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                return ["resolved OpenCode config exceeds 4 MB"]
            resolved = json.loads(raw.decode("utf-8"))
            break
        except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < _OPENCODE_RESOLVED_READ_ATTEMPTS:
                time.sleep(_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS)
    else:
        return [f"could not read resolved OpenCode agent config: {last_error}"]
    if not isinstance(expected, Mapping) or not isinstance(resolved, Mapping):
        return ["OpenCode agent config must resolve to an object"]
    return _agent_profile_errors(resolved, expected)


def _check_opencode_boundary_plugin(endpoint: str, project_dir: Path) -> list[str]:
    last_error: Exception | None = None
    for attempt in range(_OPENCODE_RESOLVED_READ_ATTEMPTS):
        try:
            query = urlencode({"directory": str(project_dir.resolve())})
            request = Request(
                f"{endpoint.rstrip('/')}/experimental/tool/ids?{query}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS) as response:  # noqa: S310 - pinned local endpoint
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                return ["resolved OpenCode tool surface exceeds 1 MB"]
            tools = json.loads(raw.decode("utf-8"))
            break
        except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < _OPENCODE_RESOLVED_READ_ATTEMPTS:
                time.sleep(_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS)
    else:
        return [f"could not read resolved OpenCode tool surface: {last_error}"]
    if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools):
        return ["resolved OpenCode tool surface must be a string list"]
    if "assurance_boundary_v1" not in tools:
        return ["resolved OpenCode tool surface is missing assurance_boundary_v1"]
    return []


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True)
    redacted = _CREDENTIAL_PATTERN.sub("[REDACTED]", text)
    value = json.loads(redacted)
    if not isinstance(value, dict):
        raise SystemExit("redacted evidence is not an object")
    return value


def _write_evidence_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    safe = _redact_evidence(payload)
    lines = [
        "# OpenCode provider-live benchmark",
        "",
        f"**Item:** `{safe.get('item_id', '')}`",
        f"**Product:** `{safe.get('product', '')}`",
        f"**Entrypoint:** `{safe.get('entrypoint', '')}`",
        f"**SUT:** `{safe.get('sut_root', '')}`",
        f"**Change ID:** `{safe.get('change_id', '')}`",
        f"**Change root:** `{safe.get('change_root', '')}`",
        f"**Started:** {safe.get('started_at', '')}",
        f"**Ended:** {safe.get('ended_at', '')}",
        f"**Terminal status:** `{safe.get('terminal_status', 'unknown')}`",
        f"**Lock digest:** `{safe.get('lock_digest', '')}`",
        f"**Outcome:** {safe.get('outcome', '')}",
        "",
        "## Routing",
        "",
        "Every prepare ID is locked to `deepseek/deepseek-v4-flash` / `max`.",
        "",
        "## Status",
        "",
        "```json",
        json.dumps(safe.get("status") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Validation",
        "",
        "```json",
        json.dumps(safe.get("validation") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Notes",
        "",
        str(safe.get("notes") or "none"),
        "",
    ]
    text = "\n".join(lines)
    _reject_credentials_in_text(text, label=str(path))
    path.write_text(text, encoding="utf-8")


def _logical_step_from_node(node_id: str, graph_id: str) -> str | None:
    name = node_id.rsplit("/", 1)[-1]
    if name != "finalize":
        return None
    prefix = node_id.rsplit("/", 1)[0] if "/" in node_id else graph_id
    if prefix.startswith("generation.") or prefix.startswith("intake."):
        return prefix
    if prefix in {"intake", "explore", "case-design", "case-review"}:
        return f"intake.{prefix}"
    if prefix in {"execute", "run"}:
        return f"execution.{prefix}"
    if prefix in {"fact-baseline", "inspect", "report", "issue-analysis", "issue-triage"}:
        return f"quality.{prefix}"
    if prefix in {"archive", "retro", "improvement-review"}:
        return f"improvement.{prefix}"
    if graph_id.startswith(("generation.", "intake.", "execution.", "quality.", "improvement.")):
        return graph_id
    return None


def _status_steps(status: Mapping[str, Any]) -> tuple[str, ...]:
    graphs = {
        item["graph_instance_id"]: item
        for item in status.get("graph_hierarchy") or []
        if isinstance(item, dict)
    }
    steps: list[str] = []
    for node in status.get("node_states") or []:
        if not isinstance(node, dict) or node.get("state") not in {"succeeded", "stopped"}:
            continue
        graph = graphs.get(node.get("graph_instance_id"), {})
        step = _logical_step_from_node(str(node.get("node_id") or ""), str(graph.get("graph_id") or ""))
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _change_is_achieved(status: Mapping[str, Any]) -> bool:
    change = status.get("change")
    return isinstance(change, Mapping) and change.get("state") == "achieved"


def _provider_reference(
    status: Mapping[str, Any], *, adapter: str, item: Mapping[str, Any]
) -> dict[str, Any]:
    session = None
    evidence = status.get("adapter_evidence") or []
    if evidence and isinstance(evidence[0], Mapping):
        session = evidence[0].get("activity_id") or evidence[0].get("activation_id")
    if adapter != "opencode":
        raise SystemExit(f"unsupported adapter {adapter!r}")
    raw_binding = item.get("adapter_binding")
    binding = raw_binding if isinstance(raw_binding, Mapping) else {}
    process = {"endpoint": binding.get("endpoint"), "protocol": "opencode-http-v1"}
    return {"session": session, "process": process}


def _validate_live_result(
    *,
    item: Mapping[str, Any],
    status: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected = str(item["expected_terminal"])
    actual = str(status.get("status") or "")
    if actual != expected:
        errors.append(f"terminal status {actual!r} != {expected!r}")
    if expected == "completed" and not _change_is_achieved(status):
        errors.append("completed status is not an achieved change")
    families = tuple(status.get("selected_test_families") or ())
    required_families = tuple(item["selected_test_families"])
    if families != required_families:
        errors.append(f"selected families {families!r} != {required_families!r}")
    steps = _status_steps(status)
    missing = [step for step in item["required_steps"] if step not in steps]
    if missing:
        errors.append(f"missing required workflow steps: {missing}")
    if required_families:
        missing_families = [
            family
            for family in required_families
            if not any(step.startswith(f"generation.{family}.") for step in steps)
        ]
        if missing_families:
            errors.append(f"absent selected-family branches: {missing_families}")
    if "quality.report" not in steps:
        errors.append("missing report")
    coverage = status.get("coverage_progress")
    if isinstance(coverage, Mapping):
        used = coverage.get("round")
        maximum = coverage.get("maximum_rounds")
        if isinstance(used, int) and isinstance(maximum, int) and used > maximum:
            errors.append("coverage-loop contract violation: rounds exceeded budget")
    if any(key in status for key in ("messages", "conversation", "session_text", "provider_text")):
        errors.append("provider session text used as status")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--item", required=True)
    parser.add_argument("--adapter", choices=("opencode",), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="Use this SUT checkout as-is (must not contain qa/). Default: a fresh SUT git worktree",
    )
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=28800)
    parser.add_argument("--nonce")
    parser.add_argument("--stamp")
    arguments = parser.parse_args(argv)

    _reject_ambient_overrides()
    repo = _repo_root()
    manifest_path = repo / "benchmark" / "assurance-product" / "manifest.json"
    document = _load_manifest(manifest_path)
    item = _manifest_item(document, arguments.item, arguments.adapter)

    started_at = _utc_now()
    stamp = arguments.stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    nonce = arguments.nonce or uuid.uuid4().hex[:8]
    change_id = derive_change_id(item_id=arguments.item, stamp=stamp, nonce=nonce)
    output = arguments.output
    if output is None:
        output = repo / "benchmark" / "assurance-product" / "results" / f"{arguments.adapter}-{stamp}-{nonce}"
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        return _fail(f"result directory must be fresh and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    try:
        sut_root = _resolve_sut(repo, str(arguments.project_dir or item["sut_root"]))
    except SystemExit as error:
        return _fail(str(error))
    if arguments.project_dir is not None and (sut_root / "qa").exists():
        return _fail("explicit project must not contain qa; retained evidence will not be overwritten")
    project_dir = sut_root
    if arguments.project_dir is None:
        try:
            project_dir = _prepare_sut_worktree(repo=repo, sut_root=sut_root, change_id=change_id)
        except SystemExit as error:
            return _fail(str(error))
        sut_root = project_dir
    change_root = project_dir / "qa"
    run_log = output / "run.log"
    run_log.touch()

    evidence: dict[str, Any] = {
        "item_id": arguments.item,
        "product": item["product"],
        "entrypoint": item["entrypoint"],
        "sut_item_id": item["sut_item_id"],
        "sut_root": str(sut_root),
        "change_id": change_id,
        "change_root": str(change_root),
        "selected_test_families": list(item["selected_test_families"]),
        "pair_id": item.get("pair_id"),
        "arm": item.get("arm"),
        "goal_baseline_digest": item.get("goal_baseline_digest"),
        "policy_digest": item.get("policy_digest"),
        "budget_digest": item.get("budget_digest"),
        "tool_versions": item.get("tool_versions"),
        "environment_digest": item.get("environment_digest"),
        "plan": None,
        "plan_ref": None,
        "terminal_outcome": None,
        "inspect_metrics": None,
        "family_conflicts": None,
        "first_round_tokens": None,
        "first_round_tool_calls": None,
        "first_round_seconds": None,
        "first_round_cost": None,
        "total_tokens": None,
        "total_tool_calls": None,
        "total_seconds": None,
        "total_cost": None,
        "coverage_rounds": None,
        "healing_rounds": None,
        "adapter_version": item["adapter_version"],
        "provider_model": next(
            iter({route.get("provider_model") for route in item["routing_assignments"].values()})
        ),
        "model_id": next(
            iter({route.get("provider_model") for route in item["routing_assignments"].values()})
        ),
        "worker_profile": next(
            iter({route.get("worker_profile") for route in item["routing_assignments"].values()})
        ),
        "started_at": started_at,
        "ended_at": None,
        "terminal_status": None,
        "lock_digest": None,
        "outcome": "incomplete",
        "notes": "",
        "status": {},
        "validation": {},
        "provider": {"session": None, "process": None},
        "logs": {"run_log": str(run_log)},
    }
    evidence_md = output / "evidence.md"

    def finish(code: int, *, notes: str, status: Mapping[str, Any] | None = None) -> int:
        evidence["ended_at"] = _utc_now()
        evidence["plan"], evidence["plan_ref"] = _acg_plan(change_root)
        if status is not None:
            evidence["status"] = {
                key: status.get(key)
                for key in (
                    "status",
                    "lock_digest",
                    "entrypoint",
                    "selected_test_families",
                    "coverage_progress",
                    "terminal_reason",
                )
            }
            evidence["terminal_status"] = status.get("status")
            evidence["terminal_outcome"] = status.get("terminal_reason") or status.get("status")
            evidence["lock_digest"] = status.get("lock_digest") or evidence.get("lock_digest")
            evidence["provider"] = _provider_reference(status, adapter=arguments.adapter, item=item)
        evidence["notes"] = notes
        # Public status does not expose a current-invocation artifact seal. A
        # retained workspace file (even with a matching change id) is not evidence.
        evidence["test_runtime_seed"] = None
        _write_json(output / "evidence.json", _redact_evidence(evidence))
        _write_evidence_markdown(evidence_md, evidence)
        return code

    env = os.environ.copy()
    secret_env = str(item["secret_env"])
    _load_opencode_secret(secret_env)
    env[secret_env] = os.environ.get(secret_env, "")
    isolated_env = dict(env)
    isolated_env.pop("PYTHONPATH", None)
    isolated_env.pop("UV_PROJECT", None)
    isolated_env["PYTHONNOUSERSITE"] = "1"

    runtime_environment_errors = _runtime_environment_errors(os.environ, output=output)
    if runtime_environment_errors:
        evidence["outcome"] = "blocked"
        return finish(1, notes="; ".join(runtime_environment_errors))

    try:
        _isolated_python, aa_next = _prepare_installed_product_env(
            repo=repo, output=output, env=env, adapter=arguments.adapter
        )
    except SystemExit as error:
        evidence["outcome"] = "blocked"
        return finish(1, notes=str(error))
    isolated_env["PATH"] = os.pathsep.join(
        (
            str(aa_next.parent),
            isolated_env.get("PATH", ""),
            "/usr/sbin",
            "/usr/bin",
            "/bin",
        )
    )

    agent_profile_errors = _project_opencode_asset_errors(project_dir)
    if agent_profile_errors:
        evidence["outcome"] = "blocked"
        return finish(
            1,
            notes="OpenCode project asset preflight failed: " + "; ".join(agent_profile_errors),
        )

    requirement_path = repo / str(item["requirement_path"])
    requirement = requirement_path.read_text(encoding="utf-8").strip()
    evidence["requirement_digest"] = _sha256(requirement.encode("utf-8"))
    spec_path = output / "run-spec.yaml"
    _write_bootstrap_spec(
        spec_path,
        item=item,
        requirement=requirement,
        timeout_seconds=arguments.timeout_seconds,
    )

    try:
        with _managed_sut_runtime(
            repo=repo,
            project_dir=project_dir,
            output=output,
            env=isolated_env,
        ) as sut_runtime:
            isolated_env.update(sut_runtime.env)
            evidence["logs"].update(
                {
                    "sut_backend": str(sut_runtime.backend_log),
                    "sut_frontend": str(sut_runtime.frontend_log),
                }
            )
            completed = _aa_next(
                aa_next,
                "bootstrap",
                "run",
                "--project-dir",
                str(project_dir),
                "--spec",
                str(spec_path),
                "--runs-root",
                str(output / "runs"),
                "--change",
                change_id,
                "--json",
                cwd=repo,
                env=isolated_env,
                timeout=max(arguments.timeout_seconds + 120, 120),
            )
            try:
                document = _parse_json(completed.stdout, label="bootstrap run")
            except SystemExit as error:
                evidence["outcome"] = "blocked"
                return finish(
                    completed.returncode or 1,
                    notes=str(error)
                    or completed.stderr.strip()
                    or completed.stdout.strip()
                    or "bootstrap run failed",
                )
            app_status = document.get("status")
            if not isinstance(app_status, dict):
                app_status = {}
            evidence["lock_digest"] = app_status.get("lock_digest") or evidence.get("lock_digest")
            errors = (
                _validate_live_result(item=item, status=app_status)
                if app_status
                else ["bootstrap status missing application status"]
            )
            evidence["validation"] = {"errors": errors, "bootstrap": document}
            if (
                errors
                or app_status.get("status") != item["expected_terminal"]
                or not _change_is_achieved(app_status)
            ):
                evidence["outcome"] = "blocked"
                detail = (
                    "; ".join(errors) if errors else f"live item did not reach {item['expected_terminal']!r}"
                )
                return finish(completed.returncode or 1, notes=detail, status=app_status)
            evidence["outcome"] = "completed"
            return finish(0, notes="live item reached achieved", status=app_status)
    except SystemExit as error:
        evidence["outcome"] = "blocked"
        return finish(1, notes=str(error))


if __name__ == "__main__":
    raise SystemExit(main())
