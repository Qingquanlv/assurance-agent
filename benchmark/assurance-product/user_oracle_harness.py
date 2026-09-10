"""Benchmark helper: materialize the User SUT and serve it for tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


FIXTURE_ROOT = Path(__file__).with_name("fixtures") / "user-oracle"
_SOURCE_MEMBERS = ("app", "migrations", "run.py", "LICENSE", "PROVENANCE.md")
_MAX_READY_TIMEOUT_S = 30.0
FAULTS = (
    "none",
    "missing-binding",
    "no-bridge",
    "no-action",
    "skip-oracle",
    "wrong-value",
    "rollback",
    "rollback-success",
    "db-unavailable",
    "wrong-environment",
    "unknown-http",
    "forged-evidence",
    "downgrade",
    "missing-write",
    "drop-business-span",
    "drop-write-span",
    "broken-context",
    "stale-trace",
    "early-completed",
    "refactor",
)


def verify_original_source(source_root: Path) -> dict[str, str]:
    lock = json.loads((FIXTURE_ROOT / "original-source-lock.json").read_bytes())
    actual = {}
    if source_root.is_symlink():
        raise ValueError("NOT_READY: original SUT source is missing or drifted")
    pending = [source_root / member for member in ("app", "migrations", "run.py")]
    while pending:
        path = pending.pop()
        if path.is_symlink():
            raise ValueError("NOT_READY: original SUT source is missing or drifted")
        if path.is_dir():
            pending.extend(sorted(path.iterdir()))
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if not path.is_file():
            raise ValueError("NOT_READY: original SUT source is missing or drifted")
        actual[path.relative_to(source_root).as_posix()] = _sha256(path)
    if actual != lock["files"]:
        raise ValueError("NOT_READY: original SUT source bytes drifted")
    return actual


def materialize_project(*, project_dir: Path, fault: str = "none") -> dict[str, Any]:
    if fault not in FAULTS:
        raise ValueError("unknown User oracle fault")
    verify_runtime_lock(FIXTURE_ROOT)
    _copy_members(FIXTURE_ROOT / "sut-source", project_dir, _SOURCE_MEMBERS)
    for name in ("requirements.in", "requirements.lock"):
        shutil.copy2(FIXTURE_ROOT / name, project_dir / name)
    frozen = project_dir / ".aa/user-oracle"
    shutil.copytree(FIXTURE_ROOT, frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if fault == "refactor":
        _apply_refactor_artifacts(project_dir, frozen / "sut-source")
    lock_path = frozen / "runtime-lock.json"
    lock = _rewrite_runtime_lock(frozen, fault=fault)
    for path in frozen.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    return {
        "files": lock["files"],
        "source_digest": lock["source_digest"],
        "runtime_digest": lock["runtime_digest"],
        "fault": fault,
        "frozen_artifact": str(frozen.resolve()),
        "frozen_artifact_digest": _sha256(lock_path),
    }


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _tree_digest(files: Mapping[str, str], prefix: str) -> str:
    selected = [[name, digest] for name, digest in sorted(files.items()) if name.startswith(prefix)]
    payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _rewrite_runtime_lock(root: Path, *, fault: str) -> dict[str, Any]:
    lock_path = root / "runtime-lock.json"
    lock = json.loads(lock_path.read_bytes())
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.relative_to(root).parts or path.suffix == ".pyc":
            continue
        if path == lock_path or path.is_dir() or path.is_symlink():
            continue
        if not path.is_file():
            continue
        actual[path.relative_to(root).as_posix()] = _sha256(path)
    lock["files"] = actual
    lock["source_digest"] = _tree_digest(actual, "sut-source/")
    lock["runtime_digest"] = _tree_digest(actual, "")
    lock["fault"] = fault
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return lock


def _apply_refactor_artifacts(project_dir: Path, frozen_source: Path) -> None:
    helper = '''from app.models.admin import User


async def persist_created_user(controller, obj_in):
    """Semantic-preserving helper: equivalent User INSERT plus an extra span."""
    from opentelemetry import trace

    with trace.get_tracer("app.users.helpers").start_as_current_span("persist_user") as span:
        span.set_attribute("aa.role", "helper")
        created = await User.create(
            username=obj_in.username,
            email=obj_in.email,
            password=obj_in.password,
            is_active=obj_in.is_active,
            is_superuser=obj_in.is_superuser,
            dept_id=obj_in.dept_id,
        )
        loaded = await controller.model.filter(id=created.id).first()
        return loaded or created
'''
    for root in (project_dir, frozen_source):
        path = root / "app/controllers/user_persist.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(helper, encoding="utf-8")
        controller = root / "app/controllers/user.py"
        text = controller.read_text(encoding="utf-8")
        if "persist_created_user" not in text:
            text = text.replace(
                "from .role import role_controller\n",
                "from .role import role_controller\nfrom .user_persist import persist_created_user\n",
            )
            text = text.replace(
                "        obj_in.password = get_password_hash(password=obj_in.password)\n"
                "        obj = await self.create(obj_in)\n"
                "        return obj\n",
                "        obj_in.password = get_password_hash(password=obj_in.password)\n"
                "        obj = await persist_created_user(self, obj_in)\n"
                "        return obj\n",
            )
            controller.write_text(text, encoding="utf-8")


def verify_runtime_lock(fixture_root: Path) -> dict[str, Any]:
    root = Path(fixture_root)
    lock_path = root / "runtime-lock.json"
    if not root.is_dir() or not lock_path.is_file():
        raise ValueError("NOT_READY: committed User oracle snapshot is missing")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("NOT_READY: User oracle runtime lock is invalid") from error
    if not isinstance(lock, dict) or lock.get("schema_version") != "1":
        raise ValueError("NOT_READY: User oracle runtime lock is invalid")
    declared = lock.get("files")
    if not isinstance(declared, dict) or not declared:
        raise ValueError("NOT_READY: User oracle runtime lock has no files")
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.relative_to(root).parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("NOT_READY: User oracle fixture contains a symlink")
        if path == lock_path or path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("NOT_READY: User oracle fixture contains a non-regular member")
        relative = path.relative_to(root).as_posix()
        actual[relative] = _sha256(path)
    if declared != actual:
        raise ValueError("NOT_READY: User oracle runtime lock does not match fixture bytes")
    source_digest = _tree_digest(actual, "sut-source/")
    runtime_digest = _tree_digest(actual, "")
    if lock.get("source_digest") != source_digest or lock.get("runtime_digest") != runtime_digest:
        raise ValueError("NOT_READY: User oracle aggregate digest does not match")
    return {"files": actual, "source_digest": source_digest, "runtime_digest": runtime_digest}


def _copy_members(source_root: Path, destination: Path, members: tuple[str, ...]) -> None:
    destination.mkdir(parents=True)
    for member in members:
        source = source_root.joinpath(*PurePosixPath(member).parts)
        target = destination.joinpath(*PurePosixPath(member).parts)
        if source.is_symlink() or not source.exists():
            raise ValueError(f"NOT_READY: snapshot member is missing: {member}")
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise ValueError(f"NOT_READY: snapshot member is not regular: {member}")


def _controlled_environment(
    *,
    python: Path,
    run_root: Path,
    sqlite_path: Path,
    instance_id: str,
    live_marker: Path,
    runtime_secrets: bool,
    ownership_token: bytes | None = None,
) -> dict[str, str]:
    home = run_root / "runtime-home"
    home.mkdir(exist_ok=True)
    values = {
        "AA_SUT_ADMIN_PASSWORD": "qualification-only",
        "AA_SUT_RESET_PASSWORD": "qualification-only",
        "AA_SUT_SECRET_KEY": "qualification-only",
    }
    if runtime_secrets:
        for name in tuple(values):
            supplied = os.environ.get(name)
            if not supplied:
                raise ValueError(f"managed SUT secret handle is missing: {name}")
            values[name] = supplied
    environment = {
        **values,
        "AA_SUT_INSTANCE_ID": instance_id,
        "AA_SUT_LIVE_MARKER": str(live_marker),
        "AA_SUT_SQLITE_PATH": str(sqlite_path),
        "AA_SUT_PREPARED_DB": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(python.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TZ": "UTC",
    }
    if ownership_token is not None:
        environment["AA_SUT_OWNERSHIP_TOKEN"] = ownership_token.hex()
    environment["PYTHONPATH"] = str(FIXTURE_ROOT)
    return environment


def _provision_runtime(run_root: Path, requirements_lock: Path, *, offline: bool) -> Path:
    runtime = Path(run_root) / "runtime"
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("NOT_READY: trusted uv provisioner is unavailable")
    cache = os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))
    environment = {
        "HOME": str(Path(run_root) / "runtime-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(Path(uv).parent),
        "TZ": "UTC",
        "UV_CACHE_DIR": cache,
        "UV_NO_CONFIG": "1",
    }
    install_command = [
        uv,
        "--no-config",
        "pip",
        "install",
        "--python",
        str(runtime / "bin" / "python"),
        "--require-hashes",
        "-r",
        str(requirements_lock),
    ]
    if offline:
        install_command.insert(2, "--offline")
    try:
        subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-m", "venv", "--without-pip", str(runtime)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
        )
        subprocess.run(  # noqa: S603
            install_command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("NOT_READY: harness-owned Python runtime provisioning failed") from error
    return runtime / "bin" / "python"


_SERVED: dict[int, subprocess.Popen[bytes]] = {}


def serve(
    project_dir: Path,
    *,
    otel_file: Path | None = None,
    ready_timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Start a shared SUT for tests/benchmarks. Does not issue ownership tokens."""
    if isinstance(ready_timeout_s, bool) or not 0 < ready_timeout_s <= _MAX_READY_TIMEOUT_S:
        raise ValueError("ready timeout must be greater than zero and at most 30 seconds")
    project = Path(project_dir).resolve()
    if not (project / "app").is_dir() or not (project / "requirements.lock").is_file():
        raise ValueError("serve() requires a materialized project")
    serve_root = project / ".serve"
    serve_root.mkdir(exist_ok=True)
    python = serve_root / "runtime" / "bin" / "python"
    if not python.exists():
        python = _provision_runtime(serve_root, project / "requirements.lock", offline=False)
    db_file = project / "db.sqlite3"
    instance_id = str(uuid.uuid4())
    live_marker = serve_root / f"live-{instance_id}.json"
    process_log = serve_root / "served-sut.log"
    throwaway_token = secrets.token_bytes(32)
    environment = _controlled_environment(
        python=python,
        run_root=serve_root,
        sqlite_path=db_file,
        instance_id=instance_id,
        live_marker=live_marker,
        runtime_secrets=False,
        ownership_token=throwaway_token,
    )
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        supplied = os.environ.get(name)
        if supplied:
            environment[name] = supplied
    lock_path = project / ".aa/user-oracle/runtime-lock.json"
    fault = "none"
    if lock_path.is_file():
        try:
            fault = str(json.loads(lock_path.read_bytes()).get("fault") or "none")
        except (OSError, json.JSONDecodeError):
            fault = "none"
    environment["AA_SUT_FAULT"] = fault
    environment["AA_SUT_FAULT_FACTS"] = str(serve_root / "fault-facts.jsonl")
    resolved_otel: str | None = None
    if otel_file is not None:
        resolved_otel = str(Path(otel_file).resolve())
        Path(resolved_otel).parent.mkdir(parents=True, exist_ok=True)
        environment["AA_SUT_OTEL_FILE"] = resolved_otel
        environment["AA_SUT_OTEL_SAMPLER"] = "always_on"
        environment["AA_SUT_OTEL_FLUSH_RECEIPT"] = str(Path(resolved_otel).parent / "otel-flush-receipt.json")
    migration = project / "migrations" / "models" / "0_20260721171822_init.py"
    if not migration.is_file():
        raise ValueError("serve() requires a materialized project with migrations")
    if not db_file.exists():
        subprocess.run(  # noqa: S603
            [
                str(python),
                str(FIXTURE_ROOT / "bootstrap.py"),
                str(db_file),
                "--migration",
                str(migration),
                "--password-env",
                "AA_SUT_ADMIN_PASSWORD",
            ],
            cwd=project,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
        )
    listener, port = reserve_loopback_socket()
    command = [
        str(python),
        "-B",
        "-m",
        "uvicorn",
        "app:app",
        "--fd",
        str(listener.fileno()),
        "--no-access-log",
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        with process_log.open("ab") as stderr:
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=environment,
                pass_fds=(listener.fileno(),),
            )
        listener.close()
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("served SUT exited before readiness")
            try:
                with urlopen(f"{base_url}/openapi.json", timeout=0.25) as response:  # noqa: S310
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.05)
        else:
            raise ValueError("served SUT readiness timed out")
    except BaseException:
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
        listener.close()
        raise
    _SERVED[process.pid] = process
    return {
        "base_url": base_url,
        "sqlite_path": str(db_file.resolve()),
        "otel_file": resolved_otel,
        "pid": process.pid,
        "instance_id": instance_id,
    }


def stop_served(pid: int) -> None:
    process = _SERVED.pop(pid, None)
    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def reserve_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener, int(listener.getsockname()[1])


def bind_loopback_port(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
    except BaseException:
        listener.close()
        raise
    return listener


def flush_otel(*, run_root: Path | None = None, otlp_path: Path | None = None) -> dict[str, Any]:
    if otlp_path is not None:
        observed = Path(otlp_path)
        traces = observed
    else:
        otel = Path(run_root) / "otel"  # type: ignore[arg-type]
        observed = otel / "observed.otlp.jsonl"
        traces = otel / "traces.jsonl"
    deadline = time.monotonic() + 8
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
        otlp = observed if observed.is_file() else traces
        if otlp.is_file() and otlp.stat().st_size > 0:
            size = otlp.stat().st_size
            try:
                load_otlp_file(otlp)
            except json.JSONDecodeError:
                last_size = size
                time.sleep(0.1)
                continue
            if size == last_size:
                stable += 1
                if stable >= 2:
                    return {"state": "flushed", "otlp_path": str(otlp)}
            else:
                stable = 0
                last_size = size
        time.sleep(0.1)
    raise ValueError("OTel flush did not produce an OTLP file")


def load_otlp_file(path: Path) -> list[dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        return []
    records: list[dict[str, Any]] = []
    try:
        loaded = json.loads(raw)
        records.append(loaded if isinstance(loaded, dict) else {"value": loaded})
        return records
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        value, index = decoder.raw_decode(raw, index)
        if isinstance(value, dict):
            records.append(value)
    return records


def flatten_spans(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kind_names = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
    spans: list[dict[str, Any]] = []
    for record in records:
        for resource in record.get("resourceSpans", [record] if "scopeSpans" in record else []):
            for scope in resource.get("scopeSpans", []):
                instrumentation = str((scope.get("scope") or {}).get("name") or "")
                for span in scope.get("spans", []):
                    attributes = {
                        item["key"]: next(iter(item.get("value", {}).values()), None)
                        for item in span.get("attributes", [])
                    }
                    normalized = {
                        "table": attributes.get("aa.db.table"),
                        "operation": attributes.get("aa.db.operation"),
                    }
                    spans.append(
                        {
                            "name": span.get("name"),
                            "kind": kind_names.get(span.get("kind"), span.get("kind")),
                            "instrumentation": instrumentation,
                            "attributes": attributes,
                            "normalized": normalized,
                            "semconv": attributes.get("aa.db.semconv"),
                            "trace_id": str(span.get("traceId") or ""),
                        }
                    )
    return spans


def normalize_db_span(span: Mapping[str, Any]) -> dict[str, str]:
    if str(FIXTURE_ROOT) not in sys.path:
        sys.path.insert(0, str(FIXTURE_ROOT))
    from bootstrap import normalize_db_attributes

    return normalize_db_attributes(dict(span.get("attributes") or {}))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--project-dir", type=Path, required=True)
    freeze_parser.add_argument("--fault", choices=FAULTS, default="none")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--project-dir", type=Path, required=True)
    serve_parser.add_argument("--otel-file", type=Path)
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("--pid", type=int, required=True)
    commands.add_parser("verify-otel-compatibility")
    return parser


def verify_otel_compatibility() -> dict[str, Any]:
    """Run the locked-SUT compatibility suite; do not fake spans from the root venv."""
    locked = verify_runtime_lock(FIXTURE_ROOT)
    if "collector.yaml" in locked["files"]:
        raise ValueError("Collector configuration must not remain in the runtime lock")
    test_file = Path(__file__).resolve().with_name("tests") / "test_user_otel_compatibility.py"
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(test_file), "-q", "--tb=short"],
        cwd=str(repo),
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("OTel compatibility tests failed against the locked User SUT")
    return {
        "schema_version": "1",
        "state": "verified",
        "command": "verify-otel-compatibility",
        "source_digest": locked["source_digest"],
        "runtime_digest": locked["runtime_digest"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "freeze":
        output = {
            "schema_version": "1",
            **materialize_project(project_dir=arguments.project_dir, fault=arguments.fault),
        }
    elif arguments.command == "serve":
        output = serve(arguments.project_dir, otel_file=arguments.otel_file)
    elif arguments.command == "stop":
        stop_served(arguments.pid)
        output = {"schema_version": "1", "state": "stopped", "pid": arguments.pid}
    else:
        output = verify_otel_compatibility()
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
