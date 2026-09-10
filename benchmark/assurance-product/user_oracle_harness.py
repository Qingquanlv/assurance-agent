"""Fixed lifecycle CLI for the managed User/SQLite benchmark SUT."""

from __future__ import annotations

import argparse
import hashlib
import hmac
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
_QUALIFIER = "qualify_runtime.py"
_OWNERSHIP_TOKEN = ".ownership-token"
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
    locked = verify_runtime_lock(FIXTURE_ROOT)
    _copy_members(FIXTURE_ROOT / "sut-source", project_dir, _SOURCE_MEMBERS)
    for name in ("requirements.in", "requirements.lock"):
        shutil.copy2(FIXTURE_ROOT / name, project_dir / name)
    frozen = project_dir / ".aa/user-oracle"
    shutil.copytree(FIXTURE_ROOT, frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    lock_path = frozen / "runtime-lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["fault"] = fault
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    for path in frozen.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    return {
        **locked,
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


def _json_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _seal(payload: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    sealed = dict(payload)
    encoded = json.dumps(sealed, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    sealed["receipt_digest"] = f"hmac-sha256:{hmac.new(key, encoded, hashlib.sha256).hexdigest()}"
    return sealed


def _authenticate_seal(payload: dict[str, Any], label: str, key: bytes) -> None:
    supplied = payload.get("receipt_digest")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_digest"}
    expected = _seal(unsigned, key)["receipt_digest"]
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise ValueError(f"{label} receipt digest does not match")


def _create_ownership_token(run_root: Path) -> bytes:
    token = secrets.token_bytes(32)
    path = run_root / _OWNERSHIP_TOKEN
    descriptor = _open_new_output(run_root, path, mode=0o400)
    try:
        os.write(descriptor, token)
    finally:
        os.close(descriptor)
    return token


def _read_ownership_token(run_root: Path) -> bytes:
    path = run_root / _OWNERSHIP_TOKEN
    if path.is_symlink() or not path.is_file():
        raise ValueError("managed SUT ownership token is missing")
    details = path.stat()
    if details.st_nlink != 1 or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("managed SUT ownership token permissions do not match")
    token = path.read_bytes()
    if len(token) != 32:
        raise ValueError("managed SUT ownership token is invalid")
    return token


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


def _source_mapping(workspace: Path, frozen: Path, files: Mapping[str, str]) -> dict[str, dict[str, str]]:
    reviewed_root = frozen.parent.parent
    return {
        (reviewed_root / name.removeprefix("sut-source/")).relative_to(workspace).as_posix(): {
            "frozen_path": name,
            "runtime_path": name.removeprefix("sut-source/"),
        }
        for name in files
        if name.startswith("sut-source/")
    }


def _authenticate_frozen_artifact(frozen: Path, digest: str | None, fault: str | None) -> dict[str, Any]:
    lock_path = frozen / "runtime-lock.json"
    if frozen != frozen.resolve() or frozen.name != "user-oracle" or frozen.parent.name != ".aa":
        raise ValueError("NOT_READY: selected frozen artifact path is invalid")
    if (
        frozen.is_symlink()
        or lock_path.is_symlink()
        or not lock_path.is_file()
        or _sha256(lock_path) != digest
    ):
        raise ValueError("NOT_READY: selected frozen artifact lock changed")
    if fault not in FAULTS or json.loads(lock_path.read_bytes()).get("fault") != fault:
        raise ValueError("NOT_READY: selected frozen artifact fault changed")
    locked = verify_runtime_lock(frozen)
    for name in ("bootstrap.py", _QUALIFIER):
        helper = FIXTURE_ROOT / name
        if helper.is_symlink() or not helper.is_file() or _sha256(helper) != locked["files"].get(name):
            raise ValueError("NOT_READY: installed lifecycle helper differs from frozen artifact")
    return locked


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
        relative = path.relative_to(sut_dir)
        if path.is_file() and not path.name.startswith("db.sqlite3") and "__pycache__" not in relative.parts:
            actual[relative.as_posix()] = _sha256(path)
    if actual != expected:
        raise ValueError("NOT_READY: materialized User oracle runtime drifted")


def _bootstrap_database(script: Path, db_file: Path, migration_file: Path) -> None:
    try:
        subprocess.run(  # noqa: S603
            [sys.executable, str(script), str(db_file), "--migration", str(migration_file)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("NOT_READY: benchmark bootstrap failed") from error


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


def _qualify_runtime(python: Path, sut_dir: Path, db_file: Path, run_root: Path) -> dict[str, Any]:
    executable = Path(python).absolute()
    expected = Path(run_root).resolve(strict=True) / "runtime" / "bin" / "python"
    if executable != expected or not executable.exists():
        raise ValueError("NOT_READY: runtime executable is not harness-provisioned")
    if executable.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise ValueError("NOT_READY: harness-provisioned Python base identity drifted")
    environment = _controlled_environment(
        python=executable,
        run_root=run_root,
        sqlite_path=db_file,
        instance_id="runtime-qualification",
        live_marker=run_root / ".qualification-marker-not-used",
        runtime_secrets=False,
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [
                str(executable),
                "-I",
                "-B",
                str(FIXTURE_ROOT / _QUALIFIER),
                str(sut_dir),
                str(db_file),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        value = json.loads(completed.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise ValueError("NOT_READY: Python runtime failed User oracle qualification") from error
    if not isinstance(value, dict) or value.get("python_executable") != str(executable):
        raise ValueError("NOT_READY: Python runtime qualification is invalid")
    value["base_python_identity"] = _file_identity(Path(sys.executable).resolve(strict=True))
    return value


def _file_identity(path: Path) -> dict[str, str | int]:
    resolved = Path(path).resolve(strict=True)
    details = resolved.stat()
    return {
        "path": str(resolved),
        "device": details.st_dev,
        "inode": details.st_ino,
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "sha256": _sha256(resolved),
    }


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


def _directory_identity(path: Path) -> dict[str, str | int]:
    resolved = path.resolve(strict=True)
    details = resolved.stat()
    return {"path": str(resolved), "device": details.st_dev, "inode": details.st_ino}


def _sqlite_identity(path: Path) -> dict[str, str | int]:
    return _directory_identity(path)


def _output_path(run_root: Path, path: Path) -> Path:
    root = Path(run_root).resolve(strict=True)
    supplied = Path(path).absolute()
    try:
        supplied.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise ValueError("managed output parent escapes the authenticated run root") from error
    return supplied


def _open_new_output(run_root: Path, path: Path, *, mode: int) -> int:
    target = _output_path(run_root, path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(target, flags, mode)
    except OSError as error:
        raise ValueError(f"managed output path is not exclusively creatable: {target.name}") from error


def _require_new_output(run_root: Path, path: Path) -> None:
    target = _output_path(run_root, path)
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError("managed output path could not be authenticated") from error
    raise ValueError(f"managed output path already exists: {target.name}")


def _write_json(run_root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    target = _output_path(run_root, path)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    descriptor = _open_new_output(run_root, temporary, mode=0o600)
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"managed output could not be replaced safely: {target.name}") from error
    details = target.stat()
    if target.is_symlink() or not target.is_file() or details.st_nlink != 1:
        raise ValueError(f"managed output is not a regular single-link file: {target.name}")


def prepare(
    *,
    workspace_root: Path,
    project_dir: Path,
    run_root: Path,
    offline: bool = False,
    fault: str = "none",
    frozen_artifact: Path | None = None,
    frozen_artifact_digest: str | None = None,
) -> dict[str, Any]:
    """Copy an explicitly frozen artifact into one exclusive managed-SUT runtime."""
    project = _confined(Path(workspace_root), Path(project_dir), "project_dir")
    run = _confined(Path(workspace_root), Path(run_root), "run_root")
    if project == run or project in run.parents or run in project.parents:
        raise ValueError("project_dir and run_root must not overlap")
    if project.exists() or run.exists():
        raise ValueError("project_dir and run_root must be new paths")
    if frozen_artifact is None:
        if frozen_artifact_digest is not None:
            raise ValueError("NOT_READY: frozen artifact selection is incomplete")
        selected = materialize_project(project_dir=project, fault=fault)
        frozen_artifact = Path(selected["frozen_artifact"])
        frozen_artifact_digest = selected["frozen_artifact_digest"]
    frozen = _confined(Path(workspace_root), frozen_artifact, "frozen_artifact")
    locked = _authenticate_frozen_artifact(frozen_artifact, frozen_artifact_digest, fault)
    if not project.exists():
        _copy_members(frozen / "sut-source", project, _SOURCE_MEMBERS)
        for name in ("requirements.in", "requirements.lock"):
            shutil.copy2(frozen / name, project / name)
    sut = run / "sut"
    _copy_members(project, sut, _RUNTIME_MEMBERS)
    _authenticate_runtime_copy(sut, locked["files"])
    db_file = sut / "db.sqlite3"
    _bootstrap_database(
        FIXTURE_ROOT / "bootstrap.py",
        db_file,
        sut / "migrations" / "models" / "0_20260721171822_init.py",
    )
    python = _provision_runtime(run, sut / "requirements.lock", offline=offline)
    qualification = _qualify_runtime(python, sut, db_file, run)
    ownership_token = _create_ownership_token(run)
    receipt = _seal(
        {
            "schema_version": "1",
            "state": "prepared",
            "fault": fault,
            "frozen_artifact": str(frozen),
            "frozen_artifact_digest": frozen_artifact_digest,
            "source_files": locked["files"],
            "source_file_mapping": _source_mapping(Path(workspace_root).resolve(), frozen, locked["files"]),
            "workspace_root": str(Path(workspace_root).resolve(strict=True)),
            "workspace_identity": _directory_identity(Path(workspace_root)),
            "project_dir": str(project),
            "run_root": str(run),
            "sut_dir": str(sut.resolve()),
            "sqlite_path": str(db_file.resolve()),
            "sqlite_identity": _sqlite_identity(db_file),
            "source_digest": locked["source_digest"],
            "runtime_digest": locked["runtime_digest"],
            "runtime_provision_mode": "offline" if offline else "locked-network",
            "runtime_qualification": qualification,
            "runtime_qualification_digest": _json_digest(qualification),
            "ownership_token_digest": f"sha256:{hashlib.sha256(ownership_token).hexdigest()}",
            "reason": "committed_snapshot_materialized",
        },
        ownership_token,
    )
    _write_json(run, run / _RECEIPT, receipt)
    return receipt


def _reserve_reusable_loopback() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, int(listener.getsockname()[1])


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
    supplied = Path(path)
    if supplied.is_symlink() or not supplied.is_file() or supplied.stat().st_nlink != 1:
        raise ValueError("harness receipt must be a regular single-link file")
    value = json.loads(supplied.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("harness receipt must be a JSON object")
    return value


def _command_fingerprint(command: list[str]) -> str:
    encoded = json.dumps(command, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _authenticated_prepare(
    workspace_root: Path, prepare_receipt: Path
) -> tuple[dict[str, Any], Path, Path, Path, Path, bytes]:
    workspace = Path(workspace_root).resolve(strict=True)
    receipt_path = Path(prepare_receipt).resolve(strict=True)
    run_root = _confined(workspace, receipt_path.parent, "run_root")
    if receipt_path != run_root / _RECEIPT:
        raise ValueError("prepare receipt path does not match the managed run root")
    ownership_token = _read_ownership_token(run_root)
    prepared = _read_json(receipt_path)
    _authenticate_seal(prepared, "prepare", ownership_token)
    if prepared.get("state") != "prepared":
        raise ValueError("managed SUT prepare receipt is not prepared")
    if prepared.get("workspace_root") != str(workspace):
        raise ValueError("prepare receipt workspace path does not match")
    if prepared.get("workspace_identity") != _directory_identity(workspace):
        raise ValueError("prepare receipt workspace identity does not match")
    if prepared.get("ownership_token_digest") != f"sha256:{hashlib.sha256(ownership_token).hexdigest()}":
        raise ValueError("prepare receipt ownership token does not match")
    sut_dir = run_root / "sut"
    db_file = sut_dir / "db.sqlite3"
    if (
        prepared.get("run_root") != str(run_root)
        or prepared.get("sut_dir") != str(sut_dir)
        or prepared.get("sqlite_path") != str(db_file)
        or prepared.get("sqlite_identity") != _sqlite_identity(db_file)
    ):
        raise ValueError("prepare receipt managed paths do not match")
    project = _confined(workspace, Path(str(prepared.get("project_dir"))), "project_dir")
    if project == run_root or project in run_root.parents or run_root in project.parents:
        raise ValueError("prepare receipt project and run roots overlap")
    selected_frozen = Path(str(prepared.get("frozen_artifact")))
    frozen = _confined(workspace, selected_frozen, "frozen_artifact")
    locked = _authenticate_frozen_artifact(
        selected_frozen, prepared.get("frozen_artifact_digest"), prepared.get("fault")
    )
    if (
        prepared.get("source_files") != locked["files"]
        or prepared.get("source_file_mapping") != _source_mapping(workspace, frozen, locked["files"])
        or prepared.get("source_digest") != locked["source_digest"]
        or prepared.get("runtime_digest") != locked["runtime_digest"]
    ):
        raise ValueError("NOT_READY: prepare receipt runtime identity drifted")
    _authenticate_runtime_copy(sut_dir, locked["files"])
    qualification = prepared.get("runtime_qualification")
    if not isinstance(qualification, dict) or prepared.get("runtime_qualification_digest") != _json_digest(
        qualification
    ):
        raise ValueError("NOT_READY: prepare receipt runtime qualification drifted")
    supplied_python = Path(str(qualification.get("python_executable")))
    python = supplied_python.parent.resolve(strict=True) / supplied_python.name
    if _qualify_runtime(python, sut_dir, db_file, run_root) != qualification:
        raise ValueError("NOT_READY: Python runtime qualification drifted")
    return prepared, run_root, sut_dir, db_file, python, ownership_token


def _collector_lock() -> dict[str, Any]:
    lock = json.loads((FIXTURE_ROOT / "runtime-lock.json").read_text(encoding="utf-8"))
    collector = lock.get("collector")
    if not isinstance(collector, dict) or collector.get("version") in {None, "latest"}:
        raise ValueError("NOT_READY: Collector lock is missing or unpinned")
    return collector


def _collector_platform() -> str:
    import platform

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "darwin_arm64"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux_amd64"
    raise ValueError(f"Collector has no locked artifact for {system}/{machine}")


def ensure_collector_artifact() -> Path:
    """Download the pinned contrib archive, verify digest, and return the binary."""
    collector = _collector_lock()
    platform_key = _collector_platform()
    artifacts = collector.get("artifacts")
    if not isinstance(artifacts, dict) or platform_key not in artifacts:
        raise ValueError("Collector artifact is not locked for this platform")
    declared = artifacts[platform_key]
    cache = Path.home() / ".cache" / "assurance-agent" / "otelcol-contrib" / f"v{collector['version']}"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(str(declared["url"])).name
    if not archive.is_file():
        from urllib.request import urlretrieve

        urlretrieve(str(declared["url"]), archive)  # noqa: S310
    digest = _sha256(archive)
    if digest != declared["digest"]:
        raise ValueError("Collector artifact digest does not match the runtime lock")
    extracted = cache / platform_key
    binary = extracted / "otelcol-contrib"
    if not binary.is_file():
        extracted.mkdir(exist_ok=True)
        subprocess.run(  # noqa: S603
            ["tar", "-xzf", str(archive), "-C", str(extracted)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("Collector binary is missing from the locked artifact")
    return binary


def _write_collector_config(
    *,
    run_root: Path,
    otlp_endpoint: str,
    health_endpoint: str,
    health_body: str,
    otlp_path: Path,
) -> Path:
    import yaml

    template = yaml.safe_load((FIXTURE_ROOT / "collector.yaml").read_text(encoding="utf-8"))
    template["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = otlp_endpoint.removeprefix("http://")
    template["extensions"]["health_check"]["endpoint"] = health_endpoint.removeprefix("http://").rstrip("/")
    template["extensions"]["health_check"]["response_body"]["healthy"] = health_body
    template["exporters"]["file"]["path"] = str(otlp_path)
    telemetry = template.setdefault("service", {}).setdefault("telemetry", {})
    telemetry.setdefault("metrics", {})["level"] = "none"
    telemetry.setdefault("logs", {})["level"] = "error"
    config_path = run_root / "otel" / "collector.yaml"
    encoded = yaml.safe_dump(template, sort_keys=False).encode("utf-8")
    descriptor = _open_new_output(run_root, config_path, mode=0o400)
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return config_path


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_health(url: str, expected: dict[str, str], timeout_s: float = 10.0) -> bytes:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5) as response:  # noqa: S310
                body = response.read()
            if json.loads(body) == expected:
                return body
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last = error
        time.sleep(0.05)
    raise ValueError("Collector health_check did not return the attempt-fixed body") from last


def start_collector(
    *,
    run_root: Path,
    execution_id: str,
    python: Path | None = None,
) -> dict[str, Any]:
    collector = _collector_lock()
    try:
        binary = ensure_collector_artifact()
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(f"Collector artifact is unavailable: {error}") from error
    otel = Path(run_root) / "otel"
    otel.mkdir(exist_ok=True)
    probe_nonce = hashlib.sha256(os.urandom(32)).hexdigest()
    health_listener, health_port = _reserve_reusable_loopback()
    otlp_listener, otlp_port = _reserve_reusable_loopback()
    health_endpoint = f"http://127.0.0.1:{health_port}/"
    otlp_endpoint = f"http://127.0.0.1:{otlp_port}"
    health_listener.close()
    otlp_listener.close()
    health_body = json.dumps(
        {"probe_nonce": probe_nonce, "execution_id": execution_id},
        separators=(",", ":"),
    )
    otlp_path = otel / "traces.jsonl"
    log_path = otel / "collector.log"
    _require_new_output(run_root, otlp_path)
    _require_new_output(run_root, log_path)
    config_path = _write_collector_config(
        run_root=run_root,
        otlp_endpoint=otlp_endpoint,
        health_endpoint=health_endpoint,
        health_body=health_body,
        otlp_path=otlp_path,
    )
    qualification_path = otel / "qualification.json"
    qualification = {
        "schema_version": "1",
        "distribution": collector["distribution"],
        "version": collector["version"],
        "sampler": collector["sampler"],
        "export_protocol": collector["export_protocol"],
        "health_extension": collector["health_extension"],
        "capture_parameters": collector["capture_parameters"],
        "instrumentors": [
            "opentelemetry.instrumentation.fastapi",
            "opentelemetry.instrumentation.tortoiseorm",
        ],
    }
    if python is not None:
        completed = subprocess.run(  # noqa: S603
            [
                str(python),
                "-c",
                "from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor\n"
                "from opentelemetry.instrumentation.tortoiseorm import TortoiseORMInstrumentor\n"
                "from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter\n"
                "print('ok')",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": str(Path(python).parent), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if completed.returncode != 0 or completed.stdout.strip() != "ok":
            raise ValueError("Collector OTel qualification failed in the locked SUT runtime")
        qualification["locked_python"] = str(python)
    _write_json(run_root, qualification_path, qualification)
    log_descriptor = _open_new_output(run_root, log_path, mode=0o600)
    try:
        with os.fdopen(log_descriptor, "ab") as stderr:
            process = subprocess.Popen(  # noqa: S603
                [str(binary), "--config", str(config_path)],
                cwd=str(otel),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env={
                    "HOME": str(Path(run_root) / "runtime-home"),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": str(binary.parent),
                    "TZ": "UTC",
                },
            )
    except OSError as error:
        raise ValueError(f"Collector failed to start: {error}") from error
    try:
        if process.poll() is not None:
            raise ValueError(
                "Collector exited before health_check: "
                + Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:]
            )
        health_bytes = _wait_health(
            health_endpoint, {"probe_nonce": probe_nonce, "execution_id": execution_id}
        )
    except BaseException:
        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:]
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise ValueError(f"Collector failed to become ready: {log_text}") from None
    birth = _process_birth_identity(process.pid)
    if birth is None:
        process.terminate()
        raise ValueError("Collector process birth identity is unavailable")
    receipt = {
        "schema_version": "1",
        "state": "started",
        "pid": process.pid,
        "process_birth_identity": birth,
        "probe_nonce": probe_nonce,
        "execution_id": execution_id,
        "health_endpoint": health_endpoint,
        "otlp_endpoint": otlp_endpoint,
        "otlp_path": str(otlp_path.resolve()),
        "log_path": str(log_path.resolve()),
        "artifact_path": str(binary.resolve(strict=True)),
        "qualification_path": str(qualification_path.resolve()),
        "dependencies_path": str((FIXTURE_ROOT / "requirements.lock").resolve(strict=True)),
        "config_path": str(config_path.resolve()),
        "sampler": collector["sampler"],
        "export_protocol": collector["export_protocol"],
        "health_extension": collector["health_extension"],
        "capture_parameters": collector["capture_parameters"],
        "endpoint_response_digest": hashlib.sha256(health_bytes).hexdigest(),
        "configuration_digest": _file_digest(config_path),
    }
    _write_json(run_root, otel / "collector-process.json", receipt)
    return receipt


def recover_trace_session(*, run_root: Path) -> dict[str, Any]:
    """Reuse the already-started Collector; never rebuild a new instance."""
    path = Path(run_root) / "otel" / "collector-process.json"
    receipt = _read_json(path)
    if receipt.get("state") != "started":
        raise ValueError("Collector session is not started")
    if _process_birth_identity(int(receipt["pid"])) != receipt.get("process_birth_identity"):
        raise ValueError("Collector process is not the owned instance")
    return receipt


def stop_collector(*, run_root: Path, drain_timeout_s: float = 5.0) -> dict[str, Any]:
    path = Path(run_root) / "otel" / "collector-process.json"
    if not path.is_file():
        raise ValueError("Collector process receipt is missing")
    receipt = _read_json(path)
    pid = receipt.get("pid")
    if not isinstance(pid, int):
        raise ValueError("Collector process identity is invalid")
    drain_state = "complete"
    try:
        if _process_birth_identity(pid) == receipt.get("process_birth_identity"):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + drain_timeout_s
            while time.monotonic() < deadline:
                if _process_birth_identity(pid) is None:
                    break
                time.sleep(0.05)
            else:
                drain_state = "incomplete"
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            drain_state = "incomplete"
    except ProcessLookupError:
        drain_state = "incomplete"
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    diagnostics = {
        "schema_version": "1",
        "drain_state": drain_state,
        "otlp_path": receipt.get("otlp_path"),
        "log_path": receipt.get("log_path"),
        "pid": pid,
    }
    diagnostics_path = Path(run_root) / "otel" / "diagnostics.json"
    if not diagnostics_path.exists():
        _write_json(run_root, diagnostics_path, diagnostics)
    stopped = {**receipt, "state": "stopped", "drain_state": drain_state}
    _write_json(run_root, Path(run_root) / "otel" / "collector-stopped.json", stopped)
    return stopped


def flush_otel(*, run_root: Path) -> dict[str, Any]:
    otlp = Path(run_root) / "otel" / "traces.jsonl"
    deadline = time.monotonic() + 8
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
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
                        }
                    )
    return spans


def normalize_db_span(span: Mapping[str, Any]) -> dict[str, str]:
    if str(FIXTURE_ROOT) not in sys.path:
        sys.path.insert(0, str(FIXTURE_ROOT))
    from bootstrap import normalize_db_attributes

    return normalize_db_attributes(dict(span.get("attributes") or {}))


def _collector_public_receipt(
    collector: Mapping[str, Any], *, sut_instance_id: str, execution_id: str
) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    readiness = {
        "schema_version": "1",
        "validation_profile": "api_db_trace.v1",
        "sut_instance_id": sut_instance_id,
        "execution_id": execution_id,
        "configuration_digest": collector["configuration_digest"],
        "authorization_scope_digest": hashlib.sha256(execution_id.encode()).hexdigest(),
        "activity_receipt_digest": hashlib.sha256(f"{execution_id}:{sut_instance_id}".encode()).hexdigest(),
        "collector_endpoint": collector["health_endpoint"],
        "collector_pid": collector["pid"],
        "collector_process_birth_identity": collector["process_birth_identity"],
        "collector_artifact": {
            "path": collector["artifact_path"],
            "digest": _file_digest(Path(collector["artifact_path"])),
        },
        "collector_config": {
            "path": collector["config_path"],
            "digest": _file_digest(Path(collector["config_path"])),
        },
        "otel_dependencies": {
            "path": collector["dependencies_path"],
            "digest": _file_digest(Path(collector["dependencies_path"])),
        },
        "otel_qualification": {
            "path": collector["qualification_path"],
            "digest": _file_digest(Path(collector["qualification_path"])),
        },
        "issued_at": now.isoformat(),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "probe_nonce": collector["probe_nonce"],
        "endpoint_response_digest": collector["endpoint_response_digest"],
    }
    return {
        "pid": collector["pid"],
        "endpoint": collector["health_endpoint"],
        "health_endpoint": collector["health_endpoint"],
        "otlp_endpoint": collector["otlp_endpoint"],
        "otlp_path": collector["otlp_path"],
        "sampler": collector["sampler"],
        "export_protocol": collector["export_protocol"],
        "health_extension": collector["health_extension"],
        "capture_parameters": collector["capture_parameters"],
        "process_birth_identity": collector["process_birth_identity"],
        "readiness": readiness,
    }


def start(
    *,
    workspace_root: Path,
    prepare_receipt: Path,
    ready_timeout_s: float = 10.0,
    validation_profile: str = "api_db.v1",
    execution_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(ready_timeout_s, bool) or not 0 < ready_timeout_s <= _MAX_READY_TIMEOUT_S:
        raise ValueError("ready timeout must be greater than zero and at most 30 seconds")
    prepared, run_root, sut_dir, db_file, python, ownership_token = _authenticated_prepare(
        Path(workspace_root), Path(prepare_receipt)
    )
    if validation_profile not in {"api_db.v1", "api_db_trace.v1"}:
        raise ValueError("NOT_READY: current API DB generation is required")
    collector_receipt: dict[str, Any] | None = None
    if validation_profile == "api_db_trace.v1":
        if (
            not isinstance(execution_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                execution_id,
            )
            is None
        ):
            raise ValueError("Collector start requires a fixed execution identity")
        collector_receipt = start_collector(run_root=run_root, execution_id=execution_id, python=python)
    process: subprocess.Popen[bytes] | None = None
    try:
        instance_id = str(uuid.uuid4())
        live_marker = run_root / f"live-{instance_id}.json"
        process_log = run_root / "managed-sut.log"
        _require_new_output(run_root, live_marker)
        _require_new_output(run_root, process_log)
        environment = _controlled_environment(
            python=python,
            run_root=run_root,
            sqlite_path=db_file,
            instance_id=instance_id,
            live_marker=live_marker,
            runtime_secrets=True,
            ownership_token=ownership_token,
        )
        environment["AA_SUT_FAULT"] = str(prepared.get("fault", "none"))
        environment["AA_SUT_FAULT_FACTS"] = str(run_root / "fault-facts.jsonl")
        if collector_receipt is not None:
            environment["AA_SUT_OTEL_ENDPOINT"] = str(collector_receipt["otlp_endpoint"])
            environment["AA_SUT_OTEL_PROTOCOL"] = str(collector_receipt["export_protocol"])
            environment["AA_SUT_OTEL_SAMPLER"] = str(collector_receipt["sampler"])
            environment["AA_SUT_OTEL_FLUSH_RECEIPT"] = str(run_root / "otel" / "flush-receipt.json")
            environment["OTEL_EXPORTER_OTLP_ENDPOINT"] = str(collector_receipt["otlp_endpoint"])
            environment["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
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
        log_descriptor = _open_new_output(run_root, process_log, mode=0o600)
        with os.fdopen(log_descriptor, "ab") as stderr:
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
        marker = _read_json(live_marker)
        marker_identity = {"instance_id": instance_id, "pid": process.pid, "sqlite_path": str(db_file)}
        marker_encoded = json.dumps(marker_identity, separators=(",", ":"), sort_keys=True).encode()
        expected_marker = {
            **marker_identity,
            "proof": f"hmac-sha256:{hmac.new(ownership_token, marker_encoded, hashlib.sha256).hexdigest()}",
        }
        if marker != expected_marker:
            raise ValueError("managed SUT live marker identity does not match")
        birth_identity = _process_birth_identity(process.pid)
        if birth_identity is None:
            raise ValueError("managed SUT process birth identity is unavailable")
        payload: dict[str, Any] = {
            "schema_version": "1",
            "state": "started",
            "workspace_root": str(Path(workspace_root).resolve(strict=True)),
            "run_root": str(run_root),
            "sut_dir": str(sut_dir),
            "prepare_receipt": str(Path(prepare_receipt).resolve(strict=True)),
            "prepare_receipt_digest": prepared["receipt_digest"],
            "prepare_receipt_sha256": hashlib.sha256(
                Path(prepare_receipt).resolve(strict=True).read_bytes()
            ).hexdigest(),
            "instance_id": instance_id,
            "pid": process.pid,
            "process_command": command,
            "process_fingerprint": _command_fingerprint(command),
            "process_birth_identity": birth_identity,
            "live_marker": str(live_marker),
            "live_marker_digest": _sha256(live_marker),
            "base_url": base_url,
            "sqlite_path": str(db_file),
            "sqlite_identity": _sqlite_identity(db_file),
            "stderr_path": str(process_log.resolve()),
            "source_digest": prepared["source_digest"],
            "runtime_digest": prepared["runtime_digest"],
            "reason": "owned_sut_started",
        }
        if collector_receipt is not None:
            payload["collector"] = _collector_public_receipt(
                collector_receipt, sut_instance_id=instance_id, execution_id=str(execution_id)
            )
        receipt = _seal(payload, ownership_token)
        _write_json(run_root, run_root / _PROCESS_RECEIPT, receipt)
    except BaseException:
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
        if collector_receipt is not None:
            stop_collector(run_root=run_root, drain_timeout_s=2.0)
        raise
    return receipt


def _process_birth_identity(pid: int) -> str | None:
    completed = subprocess.run(  # noqa: S603
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    observed = completed.stdout.strip()
    if completed.returncode != 0 or not observed:
        return None
    return f"sha256:{hashlib.sha256(observed.encode()).hexdigest()}"


def _authenticated_process(
    *, workspace_root: Path, receipt_path: Path, instance_id: str
) -> tuple[dict[str, Any], Path, Path, Path, int, bytes]:
    workspace = Path(workspace_root).resolve(strict=True)
    process_receipt_path = Path(receipt_path).resolve(strict=True)
    run_root = _confined(workspace, process_receipt_path.parent, "run_root")
    if process_receipt_path != run_root / _PROCESS_RECEIPT:
        raise ValueError("process receipt path does not match the managed run root")
    ownership_token = _read_ownership_token(run_root)
    receipt = _read_json(process_receipt_path)
    _authenticate_seal(receipt, "process", ownership_token)
    if receipt.get("state") != "started" or receipt.get("instance_id") != instance_id:
        raise ValueError("stop request does not match the owned process instance")
    prepared, expected_run, sut_dir, db_file, python, prepared_token = _authenticated_prepare(
        workspace, run_root / _RECEIPT
    )
    if not hmac.compare_digest(prepared_token, ownership_token):
        raise ValueError("process and prepare ownership tokens do not match")
    marker_path = run_root / f"live-{instance_id}.json"
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("managed SUT live marker is missing")
    if (
        expected_run != run_root
        or receipt.get("workspace_root") != str(workspace)
        or receipt.get("run_root") != str(run_root)
        or receipt.get("sut_dir") != str(sut_dir)
        or receipt.get("sqlite_path") != str(db_file)
        or receipt.get("sqlite_identity") != _sqlite_identity(db_file)
        or receipt.get("stderr_path") != str(run_root / "managed-sut.log")
        or receipt.get("prepare_receipt") != str(run_root / _RECEIPT)
        or receipt.get("prepare_receipt_digest") != prepared["receipt_digest"]
        or receipt.get("live_marker") != str(marker_path)
        or receipt.get("live_marker_digest") != _sha256(marker_path)
    ):
        raise ValueError("process receipt managed paths or live marker do not match")
    command = receipt.get("process_command")
    pid = receipt.get("pid")
    if (
        not isinstance(command, list)
        or len(command) != 8
        or command[:5] != [str(python), "-B", "-m", "uvicorn", "app:app"]
        or command[5] != "--fd"
        or command[7] != "--no-access-log"
        or not isinstance(pid, int)
        or receipt.get("process_fingerprint") != _command_fingerprint(command)
    ):
        raise ValueError("receipt does not identify an owned process")
    if receipt.get("process_birth_identity") != _process_birth_identity(pid):
        raise ValueError("running PID is not the owned process")
    marker_identity = {"instance_id": instance_id, "pid": pid, "sqlite_path": str(db_file)}
    encoded = json.dumps(marker_identity, separators=(",", ":"), sort_keys=True).encode()
    if _read_json(marker_path) != {
        **marker_identity,
        "proof": "hmac-sha256:" + hmac.new(ownership_token, encoded, hashlib.sha256).hexdigest(),
    }:
        raise ValueError("managed SUT live marker proof does not match")
    return receipt, run_root, process_receipt_path, marker_path, pid, ownership_token


def preflight(*, workspace_root: Path, receipt_path: Path, instance_id: str) -> dict[str, Any]:
    """Authenticate the currently owned SUT without writing or stopping anything."""
    from http.client import HTTPConnection
    from urllib.parse import urlsplit

    receipt, _, _, _, pid, _ = _authenticated_process(
        workspace_root=workspace_root, receipt_path=receipt_path, instance_id=instance_id
    )
    url = urlsplit(str(receipt.get("base_url")))
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.port is None
        or url.path
        or url.query
        or url.fragment
        or url.username is not None
    ):
        raise ValueError("managed SUT readiness URL is invalid")
    connection = HTTPConnection("127.0.0.1", url.port, timeout=2)
    try:
        connection.request("GET", "/openapi.json")
        if connection.getresponse().status != 200:
            raise ValueError("managed SUT is not ready")
    finally:
        connection.close()
    if receipt["process_birth_identity"] != _process_birth_identity(pid):
        raise ValueError("managed SUT process changed during readiness")
    return {
        "schema_version": "1",
        "state": "ready",
        "instance_id": instance_id,
        "start_receipt_sha256": _sha256(receipt_path),
    }


def stop(
    *, workspace_root: Path, receipt_path: Path, instance_id: str, timeout_s: float = 5.0
) -> dict[str, Any]:
    receipt, run_root, process_receipt_path, marker_path, pid, ownership_token = _authenticated_process(
        workspace_root=workspace_root, receipt_path=receipt_path, instance_id=instance_id
    )
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
    stopped = _seal(
        {
            **{key: value for key, value in receipt.items() if key != "receipt_digest"},
            "state": "stopped",
            "reason": "owned_sut_stopped",
        },
        ownership_token,
    )
    marker_path.unlink(missing_ok=True)
    if isinstance(receipt.get("collector"), dict):
        try:
            stop_collector(run_root=run_root, drain_timeout_s=5.0)
        except ValueError:
            pass
    _write_json(run_root, run_root / "stopped-process.json", stopped)
    return stopped


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--project-dir", type=Path, required=True)
    freeze_parser.add_argument("--fault", choices=FAULTS, default="none")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--workspace-root", type=Path, required=True)
    prepare_parser.add_argument("--project-dir", type=Path, required=True)
    prepare_parser.add_argument("--run-root", type=Path, required=True)
    prepare_parser.add_argument("--offline", action="store_true")
    prepare_parser.add_argument("--frozen-artifact", type=Path)
    prepare_parser.add_argument("--frozen-artifact-digest")
    prepare_parser.add_argument("--fault", choices=FAULTS, default="none")
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--workspace-root", type=Path, required=True)
    start_parser.add_argument("--prepare-receipt", type=Path, required=True)
    start_parser.add_argument("--validation-profile", default="api_db.v1")
    start_parser.add_argument("--execution-id")
    commands.add_parser("verify-otel-compatibility")
    for name in ("stop", "preflight"):
        process_parser = commands.add_parser(name)
        process_parser.add_argument("--workspace-root", type=Path, required=True)
        process_parser.add_argument("--receipt", type=Path, required=True)
        process_parser.add_argument("--instance-id", required=True)
    return parser


def verify_otel_compatibility() -> dict[str, Any]:
    """Run the locked-SUT compatibility suite; do not fake spans from the root venv."""
    ensure_collector_artifact()
    locked = verify_runtime_lock(FIXTURE_ROOT)
    if "collector.yaml" not in locked["files"]:
        raise ValueError("Collector configuration is not in the runtime lock")
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
    elif arguments.command == "prepare":
        output = prepare(
            workspace_root=arguments.workspace_root,
            project_dir=arguments.project_dir,
            run_root=arguments.run_root,
            offline=arguments.offline,
            fault=arguments.fault,
            frozen_artifact=arguments.frozen_artifact,
            frozen_artifact_digest=arguments.frozen_artifact_digest,
        )
    elif arguments.command == "start":
        output = start(
            workspace_root=arguments.workspace_root,
            prepare_receipt=arguments.prepare_receipt,
            validation_profile=arguments.validation_profile,
            execution_id=arguments.execution_id,
        )
    elif arguments.command == "verify-otel-compatibility":
        output = verify_otel_compatibility()
    else:
        operation = preflight if arguments.command == "preflight" else stop
        output = operation(
            workspace_root=arguments.workspace_root,
            receipt_path=arguments.receipt,
            instance_id=arguments.instance_id,
        )
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
