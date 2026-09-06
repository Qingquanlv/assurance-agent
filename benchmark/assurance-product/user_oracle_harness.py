"""Fixed lifecycle CLI for the managed User/SQLite benchmark SUT."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
_RUNTIME_MEMBERS = (*_SOURCE_MEMBERS, "requirements.in", "requirements.lock")
_RECEIPT = "harness-prepare.json"
_PROCESS_RECEIPT = "owned-process.json"


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _tree_digest(files: Mapping[str, str], prefix: str) -> str:
    selected = [[name, digest] for name, digest in sorted(files.items()) if name.startswith(prefix)]
    payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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
        if path == lock_path or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
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


def _confined(workspace_root: Path, target: Path, label: str) -> Path:
    workspace = workspace_root.resolve(strict=True)
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError as error:
        raise ValueError(f"{label} must stay inside the selected worktree") from error
    if resolved == workspace:
        raise ValueError(f"{label} must be below the selected worktree")
    return resolved


def _copy_members(source_root: Path, destination: Path, members: tuple[str, ...]) -> None:
    destination.mkdir(parents=True)
    for member in members:
        source = source_root.joinpath(*PurePosixPath(member).parts)
        target = destination.joinpath(*PurePosixPath(member).parts)
        if source.is_symlink() or not source.exists():
            raise ValueError(f"NOT_READY: snapshot member is missing: {member}")
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise ValueError(f"NOT_READY: snapshot member is not regular: {member}")


def _authenticate_runtime_copy(sut_dir: Path, locked_files: Mapping[str, str]) -> None:
    expected = {
        name.removeprefix("sut-source/"): digest
        for name, digest in locked_files.items()
        if name.startswith("sut-source/") or name in {"requirements.in", "requirements.lock"}
    }
    actual: dict[str, str] = {}
    for path in sorted(sut_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError("NOT_READY: materialized User oracle runtime contains a symlink")
        if path.is_file() and path.name != "db.sqlite3":
            actual[path.relative_to(sut_dir).as_posix()] = _sha256(path)
    if actual != expected:
        raise ValueError("NOT_READY: materialized User oracle runtime drifted")


def _bootstrap_database(script: Path, db_file: Path) -> None:
    try:
        subprocess.run(  # noqa: S603
            [sys.executable, str(script), str(db_file)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("NOT_READY: benchmark bootstrap failed") from error


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare(
    *,
    workspace_root: Path,
    project_dir: Path,
    run_root: Path,
) -> dict[str, Any]:
    """Materialize one project and one exclusive managed-SUT runtime from the snapshot."""
    locked = verify_runtime_lock(FIXTURE_ROOT)
    project = _confined(Path(workspace_root), Path(project_dir), "project_dir")
    run = _confined(Path(workspace_root), Path(run_root), "run_root")
    if project.exists() or run.exists():
        raise ValueError("project_dir and run_root must be new paths")
    source = FIXTURE_ROOT / "sut-source"
    _copy_members(source, project, _SOURCE_MEMBERS)
    for name in ("requirements.in", "requirements.lock"):
        shutil.copy2(FIXTURE_ROOT / name, project / name)
    sut = run / "sut"
    _copy_members(project, sut, _RUNTIME_MEMBERS)
    _authenticate_runtime_copy(sut, locked["files"])
    db_file = sut / "db.sqlite3"
    _bootstrap_database(FIXTURE_ROOT / "bootstrap.py", db_file)
    receipt: dict[str, Any] = {
        "schema_version": "1",
        "state": "prepared",
        "project_dir": str(project),
        "run_root": str(run),
        "sut_dir": str(sut.resolve()),
        "sqlite_path": str(db_file.resolve()),
        "source_digest": locked["source_digest"],
        "runtime_digest": locked["runtime_digest"],
        "reason": "committed_snapshot_materialized",
    }
    _write_json(run / _RECEIPT, receipt)
    return receipt


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


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("harness receipt must be a JSON object")
    return value


def _command_fingerprint(command: list[str]) -> str:
    encoded = json.dumps(command, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def start(
    *,
    prepare_receipt: Path,
    python_executable: Path,
    ready_timeout_s: float = 10.0,
) -> dict[str, Any]:
    prepared = _read_json(Path(prepare_receipt))
    if prepared.get("state") != "prepared":
        raise ValueError("managed SUT prepare receipt is not prepared")
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        if not os.environ.get(name):
            raise ValueError(f"managed SUT secret handle is missing: {name}")
    sut_dir = Path(str(prepared["sut_dir"])).resolve(strict=True)
    db_file = Path(str(prepared["sqlite_path"])).resolve(strict=True)
    python = Path(python_executable).resolve(strict=True)
    locked = verify_runtime_lock(FIXTURE_ROOT)
    if (
        prepared.get("source_digest") != locked["source_digest"]
        or prepared.get("runtime_digest") != locked["runtime_digest"]
    ):
        raise ValueError("NOT_READY: prepare receipt runtime identity drifted")
    _authenticate_runtime_copy(sut_dir, locked["files"])
    subprocess.run(  # noqa: S603
        [
            str(python),
            str(FIXTURE_ROOT / "bootstrap.py"),
            str(db_file),
            "--password-env",
            "AA_SUT_ADMIN_PASSWORD",
        ],
        cwd=sut_dir,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    listener, port = reserve_loopback_socket()
    command = [
        str(python),
        "-m",
        "uvicorn",
        "app:app",
        "--fd",
        str(listener.fileno()),
        "--no-access-log",
    ]
    environment = dict(os.environ)
    environment["AA_SUT_PORT"] = str(port)
    process_log = Path(str(prepared["run_root"])) / "managed-sut.log"
    with process_log.open("ab") as stderr:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=sut_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=environment,
            pass_fds=(listener.fileno(),),
        )
    listener.close()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + ready_timeout_s
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("managed SUT exited before readiness")
            try:
                with urlopen(f"{base_url}/openapi.json", timeout=0.25) as response:  # noqa: S310
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.05)
        else:
            raise ValueError("managed SUT readiness timed out")
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise
    instance_id = str(uuid.uuid4())
    receipt = {
        "schema_version": "1",
        "state": "started",
        "instance_id": instance_id,
        "pid": process.pid,
        "process_command": command,
        "process_fingerprint": _command_fingerprint(command),
        "base_url": base_url,
        "sqlite_path": str(db_file),
        "stderr_path": str(process_log.resolve()),
        "source_digest": prepared["source_digest"],
        "runtime_digest": prepared["runtime_digest"],
        "reason": "owned_sut_started",
    }
    _write_json(Path(str(prepared["run_root"])) / _PROCESS_RECEIPT, receipt)
    return receipt


def _process_command(pid: int) -> str:
    completed = subprocess.run(  # noqa: S603
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def stop(*, receipt_path: Path, instance_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    receipt = _read_json(Path(receipt_path))
    if receipt.get("state") != "started" or receipt.get("instance_id") != instance_id:
        raise ValueError("stop request does not match the owned process instance")
    command = receipt.get("process_command")
    pid = receipt.get("pid")
    if (
        not isinstance(command, list)
        or len(command) != 7
        or command[1:4] != ["-m", "uvicorn", "app:app"]
        or command[4] != "--fd"
        or command[6] != "--no-access-log"
        or not isinstance(pid, int)
        or receipt.get("process_fingerprint") != _command_fingerprint(command)
    ):
        raise ValueError("receipt does not identify an owned process")
    observed = _process_command(pid)
    if "uvicorn app:app" not in observed or Path(command[0]).name not in observed:
        raise ValueError("running PID is not the owned process")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = 0
        if waited == pid:
            break
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise ValueError("owned process did not stop within budget")
    stopped = {**receipt, "state": "stopped", "reason": "owned_sut_stopped"}
    _write_json(Path(receipt_path), stopped)
    return stopped


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--workspace-root", type=Path, required=True)
    prepare_parser.add_argument("--project-dir", type=Path, required=True)
    prepare_parser.add_argument("--run-root", type=Path, required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--prepare-receipt", type=Path, required=True)
    start_parser.add_argument("--python", type=Path, required=True)
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("--receipt", type=Path, required=True)
    stop_parser.add_argument("--instance-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        output = prepare(
            workspace_root=arguments.workspace_root,
            project_dir=arguments.project_dir,
            run_root=arguments.run_root,
        )
    elif arguments.command == "start":
        output = start(
            prepare_receipt=arguments.prepare_receipt,
            python_executable=arguments.python,
        )
    else:
        output = stop(receipt_path=arguments.receipt, instance_id=arguments.instance_id)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
