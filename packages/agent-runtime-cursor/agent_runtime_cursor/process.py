from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast

from pydantic import Field

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import (
    canonical_digest,
    canonical_json_bytes,
    reject_credentials_in_digest_input,
)
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import FrozenModel, SecretHandleUnauthorized, TaskContext

from agent_runtime_cursor.config import PROTOCOL_PROFILE, CursorAdapterConfig

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ACCEPTABLE_CONFINEMENT = frozenset({"process-group", "job-object", "cgroup", "container"})
_PINNED_ARGV = ("agent", "--print", "--output-format", "stream-json", "--force")
_SHEBANG_PATH = ("/usr/bin", "/bin")


class ConfinementIdentity(FrozenModel):
    mechanism: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    descendant_inheritance: bool
    host_boot_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    host_instance_id: str = Field(min_length=1)
    executable_version: str = Field(min_length=1)


class CursorProcessReceipt(FrozenModel):
    host_boot_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    host_instance_id: str = Field(min_length=1)
    confinement_identity: str = Field(min_length=1)
    process_group_identity: str = Field(min_length=1)
    process_start_token: str = Field(min_length=1)
    executable_version_digest: str = Field(pattern=_SHA256_PATTERN)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    argv_policy_digest: str = Field(pattern=_SHA256_PATTERN)
    workspace_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    started_at: float
    stream_session_id: str | None = None


class ProcessObservation(FrozenModel):
    status: Literal["running", "exited", "unknown"]
    exit_code: int | None = None


class CancelPolicy(FrozenModel):
    graceful_seconds: float = Field(gt=0, le=60)
    forced_seconds: float = Field(gt=0, le=60)


@dataclass(frozen=True, slots=True)
class ProcessLaunchRequest:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdin: bytes
    shell: bool
    executable_version_digest: str
    request_digest: str
    argv_policy_digest: str
    workspace_identity_digest: str


@dataclass(frozen=True, slots=True)
class ConfinedProcess:
    receipt: CursorProcessReceipt


@dataclass(frozen=True, slots=True)
class HostTerminalResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float


class ConfinedProcessHost(Protocol):
    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity: ...

    def authenticate(self, receipt: CursorProcessReceipt) -> None: ...

    def unbound_spawn_state(
        self, fingerprint: Mapping[str, object]
    ) -> Literal["not_spawned", "spawned", "unknown"]: ...

    def read_durable_terminal(self, receipt: CursorProcessReceipt) -> HostTerminalResult | None: ...

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess: ...

    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation: ...

    async def wait(self, receipt: CursorProcessReceipt) -> HostTerminalResult: ...

    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None: ...


def authenticate_executable(config: CursorAdapterConfig) -> Path:
    path = Path(config.executable)
    if path.is_symlink() or not path.is_file():
        raise ValueError("executable identity must be a regular file")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("executable identity must be a regular file")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != config.executable_digest:
        raise ValueError("executable digest does not match the locked identity")
    return path


def authenticate_confinement(
    identity: ConfinementIdentity,
    *,
    expected_version: str,
) -> ConfinementIdentity:
    if identity.mechanism not in _ACCEPTABLE_CONFINEMENT or not identity.descendant_inheritance:
        raise TaskActivityProtocolViolation(
            "confinement is insufficient: pid plus finally kill does not prove descendant inheritance"
        )
    if identity.executable_version != expected_version:
        if identity.executable_version != canonical_digest(expected_version):
            raise ValueError("reported version does not match the locked executable version")
    return identity


def argv_policy_document(argv: tuple[str, ...], environment_names: tuple[str, ...]) -> dict[str, object]:
    return {
        "argv": list(argv),
        "cwd_policy": "attempt-workspace",
        "environment_names": sorted(environment_names),
        "shell": False,
        "stdin": "canonical-agent-run-request",
    }


def workspace_identity_digest_for(context: TaskContext) -> str:
    return canonical_digest({"cwd": str(context.workspace_root.resolve())})


def cursor_dispatch_fingerprint(
    config: CursorAdapterConfig,
    argv: tuple[str, ...],
    *,
    request_digest: str,
    workspace_identity_digest: str,
    host_boot_identity_digest: str,
    host_instance_id: str,
    attempt: int,
    task_id: str,
) -> dict[str, object]:
    fingerprint: dict[str, object] = {
        "protocol_profile": PROTOCOL_PROFILE,
        "executable_digest": config.executable_digest,
        "executable_version_digest": canonical_digest(config.expected_version),
        "argv_policy_digest": canonical_digest(argv_policy_document(argv, config.environment_names)),
        "request_digest": request_digest,
        "workspace_identity_digest": workspace_identity_digest,
        "host_boot_identity_digest": host_boot_identity_digest,
        "host_instance_id": host_instance_id,
        "request_identity_digest": canonical_digest({"attempt": attempt, "task_id": task_id}),
    }
    reject_credentials_in_digest_input(fingerprint)
    return fingerprint


def _resolve_environment(
    config: CursorAdapterConfig, *, executable: Path, context: TaskContext
) -> Mapping[str, str]:
    resolved: dict[str, str] = {}
    for name in config.environment_names:
        if name == "PATH":
            resolved[name] = os.pathsep.join((str(executable.parent), *_SHEBANG_PATH))
            continue
        if name != "CURSOR_API_KEY" or config.secret_handle is None:
            raise ValueError("environment_names contains an unresolvable entry")
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        resolved[name] = context.secrets.resolve(config.secret_handle).decode("utf-8")
    return MappingProxyType(resolved)


def build_launch_request(
    config: CursorAdapterConfig,
    agent_run: AgentRunRequest,
    context: TaskContext,
    executable: Path,
) -> ProcessLaunchRequest:
    argv = (str(executable), *_PINNED_ARGV)
    if agent_run.execution.provider_model != "provider_default":
        argv = (*argv, "--model", agent_run.execution.provider_model)
    if any(part == "--resume" or part.startswith("--resume=") for part in argv):
        raise ValueError("argv must not include resume selection")
    policy = argv_policy_document(argv, config.environment_names)
    request_digest = canonical_digest(agent_run.model_dump(mode="json"))
    workspace_digest = workspace_identity_digest_for(context)
    return ProcessLaunchRequest(
        argv=argv,
        cwd=context.workspace_root.resolve(),
        environment=_resolve_environment(config, executable=executable, context=context),
        stdin=canonical_json_bytes(agent_run.model_dump(mode="json")),
        shell=False,
        executable_version_digest=canonical_digest(config.expected_version),
        request_digest=request_digest,
        argv_policy_digest=canonical_digest(policy),
        workspace_identity_digest=workspace_digest,
    )


class UnsupportedCursorPlatform(ValueError):
    """Raised when Cursor confined execution supports Linux and macOS only."""


_HOST_DIR_NAME = ".cursor-process-host"
_HOST_STATE_NAME = "host-state.json"
_SPAWNS_DIR_NAME = "spawns"
_TERMINALS_DIR_NAME = "terminals"
_HEARTBEATS_DIR_NAME = "heartbeats"
_MAX_CAPTURE_BYTES = 16_000_000
_HEARTBEAT_REFRESH_SECONDS = 0.25
_STREAM_READ_CHUNK_BYTES = 4096
_TERMINATE_POLL_SECONDS = 0.01


def production_process_host(root: Path) -> ConfinedProcessHost:
    if sys.platform == "darwin":
        return MacOSProcessGroupHost(root)
    if sys.platform.startswith("linux"):
        return LinuxProcessSupervisorHost(root)
    raise UnsupportedCursorPlatform("Cursor execution supports Linux and macOS only")


def _host_dir(root: Path) -> Path:
    return Path(root).resolve() / _HOST_DIR_NAME


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(dict(payload))
    temp = path.with_suffix(f"{path.suffix}.tmp")
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _read_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TaskActivityProtocolViolation("host state is corrupt")
    return cast(dict[str, object], document)


def _load_or_create_host_state(host_dir: Path) -> tuple[str, bytes, str]:
    state_path = host_dir / _HOST_STATE_NAME
    if state_path.is_file():
        state = _read_json(state_path)
        boot = state.get("host_boot_identity_digest")
        key = state.get("mac_key")
        if not isinstance(boot, str) or not isinstance(key, str):
            raise TaskActivityProtocolViolation("host state is corrupt")
        return boot, bytes.fromhex(key), boot
    boot_seed = canonical_digest({"host_root": str(host_dir.parent.resolve())})
    mac_key = os.urandom(32)
    boot_identity = canonical_digest({"seed": boot_seed, "mac_key": mac_key.hex()})
    _write_json_atomically(
        state_path,
        {
            "host_boot_identity_digest": boot_identity,
            "mac_key": mac_key.hex(),
        },
    )
    return boot_identity, mac_key, boot_identity


def _receipt_mac(mac_key: bytes, receipt: CursorProcessReceipt) -> str:
    payload = receipt.model_dump(mode="json")
    return hmac.new(mac_key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _heartbeat_mac(mac_key: bytes, receipt: CursorProcessReceipt, updated_at: float) -> str:
    payload = {
        "receipt": receipt.model_dump(mode="json"),
        "updated_at": updated_at,
    }
    return hmac.new(mac_key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _terminal_mac(mac_key: bytes, receipt: CursorProcessReceipt, terminal: HostTerminalResult) -> str:
    payload = {
        "receipt": receipt.model_dump(mode="json"),
        "terminal": {
            "exit_code": terminal.exit_code,
            "stdout": terminal.stdout.hex(),
            "stderr": terminal.stderr.hex(),
            "elapsed_seconds": terminal.elapsed_seconds,
        },
    }
    return hmac.new(mac_key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _terminal_from_payload(payload: Mapping[str, object]) -> HostTerminalResult:
    return HostTerminalResult(
        exit_code=int(payload["exit_code"]),
        stdout=bytes.fromhex(str(payload["stdout"])),
        stderr=bytes.fromhex(str(payload["stderr"])),
        elapsed_seconds=float(payload["elapsed_seconds"]),
    )


def _validate_launch_request(request: ProcessLaunchRequest) -> None:
    if request.shell:
        raise TaskActivityProtocolViolation("confinement forbids shell execution")
    if not request.argv:
        raise TaskActivityProtocolViolation("argv must not be empty")
    executable = Path(request.argv[0])
    if executable.is_symlink() or not executable.is_file():
        raise TaskActivityProtocolViolation("executable identity must be a regular file")
    mode = executable.stat().st_mode
    if not stat.S_ISREG(mode):
        raise TaskActivityProtocolViolation("executable identity must be a regular file")
    cwd = request.cwd.resolve()
    if not cwd.is_dir():
        raise TaskActivityProtocolViolation("attempt workspace is unavailable")
    expected_workspace = canonical_digest({"cwd": str(cwd)})
    if request.workspace_identity_digest != expected_workspace:
        raise TaskActivityProtocolViolation("workspace identity drifted from the attempt cwd")
    policy = argv_policy_document(request.argv, tuple(sorted(request.environment)))
    if canonical_digest(policy) != request.argv_policy_digest:
        raise TaskActivityProtocolViolation("argv policy is not authentic")
    allowed = set(policy["environment_names"])  # type: ignore[index]
    if set(request.environment) != allowed:
        raise TaskActivityProtocolViolation("environment names drifted from argv policy")


def _spawn_environment(request: ProcessLaunchRequest) -> Mapping[str, str]:
    resolved = dict(request.environment)
    resolved["PYTHONUNBUFFERED"] = "1"
    if "HOME" not in resolved:
        resolved["HOME"] = str(request.cwd.resolve())
    return MappingProxyType(resolved)


def _collect_process_tree(root_pid: int) -> tuple[int, ...]:
    try:
        completed = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "ppid="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return (root_pid,)
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    collected: list[int] = []

    def walk(pid: int) -> None:
        collected.append(pid)
        for child in children.get(pid, []):
            walk(child)

    walk(root_pid)
    return tuple(dict.fromkeys(collected))


def _bounded_stream_reader(
    stream: object | None,
    chunks: list[bytes],
    *,
    max_bytes: int,
    total: list[int],
) -> None:
    if stream is None:
        return
    while total[0] < max_bytes:
        remaining = max_bytes - total[0]
        data = stream.read(min(_STREAM_READ_CHUNK_BYTES, remaining))  # type: ignore[attr-defined]
        if not data:
            break
        chunks.append(data)
        total[0] += len(data)


def _collect_process_output_bounded(
    process: subprocess.Popen[bytes],
    *,
    max_bytes: int,
) -> tuple[bytes, bytes, int]:
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_total = [0]
    stderr_total = [0]

    def read_stdout() -> None:
        _bounded_stream_reader(
            process.stdout,
            stdout_chunks,
            max_bytes=max_bytes,
            total=stdout_total,
        )

    def read_stderr() -> None:
        _bounded_stream_reader(
            process.stderr,
            stderr_chunks,
            max_bytes=max_bytes,
            total=stderr_total,
        )

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    exit_code = process.wait()
    stdout_thread.join(timeout=5.0)
    stderr_thread.join(timeout=5.0)
    return b"".join(stdout_chunks), b"".join(stderr_chunks), exit_code


def _close_process_handles(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _any_process_alive(pids: tuple[int, ...]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return True
        else:
            return True
    return False


class _BaseProductionConfinedProcessHost:
    __slots__ = (
        "_boot_identity_digest",
        "_host_dir",
        "_host_instance_id",
        "_mac_key",
        "_root",
        "_spawn_count",
    )

    _confinement_mechanism = "process-group"

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._host_dir = _host_dir(self._root)
        self._host_instance_id = uuid.uuid4().hex
        self._boot_identity_digest, self._mac_key, _ = _load_or_create_host_state(self._host_dir)
        self._spawn_count = 0
        if not hasattr(self, "_processes"):
            self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity:
        _validate_launch_request(request)
        return ConfinementIdentity(
            mechanism=self._confinement_mechanism,
            identity=f"host:{self._host_instance_id}",
            descendant_inheritance=True,
            host_boot_identity_digest=self._boot_identity_digest,
            host_instance_id=self._host_instance_id,
            executable_version=request.executable_version_digest,
        )

    def authenticate(self, receipt: CursorProcessReceipt) -> None:
        if receipt.host_boot_identity_digest != self._boot_identity_digest:
            raise TaskActivityProtocolViolation("process receipt does not match this host")
        record = self._load_spawn_record(receipt.process_start_token)
        if record is None:
            raise TaskActivityProtocolViolation("process receipt does not match this host")
        if _receipt_mac(self._mac_key, receipt) != record["receipt_mac"]:
            raise TaskActivityProtocolViolation("process receipt does not match this host")
        stored = CursorProcessReceipt.model_validate(record["receipt"])
        if stored != receipt:
            raise TaskActivityProtocolViolation("process receipt does not match this host")
        if self._terminal_path(receipt.process_start_token).is_file():
            return
        if not self._start_identity_matches(record):
            raise TaskActivityProtocolViolation("process receipt does not match this host")

    def unbound_spawn_state(
        self, fingerprint: Mapping[str, object]
    ) -> Literal["not_spawned", "spawned", "unknown"]:
        if (
            fingerprint.get("host_instance_id") != self._host_instance_id
            or fingerprint.get("host_boot_identity_digest") != self._boot_identity_digest
        ):
            return "unknown"
        request_digest = fingerprint.get("request_digest")
        if isinstance(request_digest, str) and self._spawn_record_for_request(request_digest) is not None:
            return "spawned"
        if self._spawn_count > 0:
            return "spawned"
        return "not_spawned"

    def read_durable_terminal(self, receipt: CursorProcessReceipt) -> HostTerminalResult | None:
        self.authenticate(receipt)
        terminal_path = self._terminal_path(receipt.process_start_token)
        if not terminal_path.is_file():
            return None
        payload = _read_json(terminal_path)
        stored_mac = payload.get("receipt_mac")
        terminal_payload = payload.get("terminal")
        if not isinstance(stored_mac, str) or not isinstance(terminal_payload, dict):
            raise TaskActivityProtocolViolation("host terminal receipt is not authentic")
        terminal = _terminal_from_payload(terminal_payload)
        if _terminal_mac(self._mac_key, receipt, terminal) != stored_mac:
            raise TaskActivityProtocolViolation("host terminal receipt is not authentic")
        return terminal

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess:
        identity = self.preflight(request)
        env = dict(_spawn_environment(request))
        started_at = time.time()
        process = subprocess.Popen(
            list(request.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(request.cwd.resolve()),
            env=env,
            shell=False,
            start_new_session=True,
        )
        if process.stdin is not None:
            if request.stdin:
                process.stdin.write(request.stdin)
            process.stdin.close()
        try:
            process_group = os.getpgid(process.pid)
        except ProcessLookupError:
            process_group = process.pid
        confinement_identity = self._confinement_identity(process_group)
        start_identity = self._read_start_identity(process.pid)
        receipt = CursorProcessReceipt(
            host_boot_identity_digest=identity.host_boot_identity_digest,
            host_instance_id=identity.host_instance_id,
            confinement_identity=confinement_identity,
            process_group_identity=f"pgid:{process_group}",
            process_start_token=uuid.uuid4().hex,
            executable_version_digest=request.executable_version_digest,
            request_digest=request.request_digest,
            argv_policy_digest=request.argv_policy_digest,
            workspace_identity_digest=request.workspace_identity_digest,
            started_at=started_at,
        )
        receipt_mac = _receipt_mac(self._mac_key, receipt)
        self._persist_spawn_record(
            receipt=receipt,
            pid=process.pid,
            process_group=process_group,
            start_identity=start_identity,
            receipt_mac=receipt_mac,
        )
        self._spawn_count += 1
        self._track_process(receipt.process_start_token, process)
        self._write_progress_heartbeat(receipt)
        return ConfinedProcess(receipt=receipt)

    def read_progress_heartbeat(self, receipt: CursorProcessReceipt) -> float | None:
        self.authenticate(receipt)
        heartbeat_path = self._heartbeat_path(receipt.process_start_token)
        if not heartbeat_path.is_file():
            return None
        payload = _read_json(heartbeat_path)
        updated_at = payload.get("updated_at")
        stored_mac = payload.get("receipt_mac")
        if not isinstance(updated_at, (int, float)) or not isinstance(stored_mac, str):
            raise TaskActivityProtocolViolation("progress heartbeat is not authentic")
        if _heartbeat_mac(self._mac_key, receipt, float(updated_at)) != stored_mac:
            raise TaskActivityProtocolViolation("progress heartbeat is not authentic")
        return float(updated_at)

    def close(self) -> None:
        for token in list(self._processes):
            self._reap_tracked_process(token)

    def __enter__(self) -> _BaseProductionConfinedProcessHost:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation:
        self.authenticate(receipt)
        durable = self.read_durable_terminal(receipt)
        if durable is not None:
            return ProcessObservation(status="exited", exit_code=durable.exit_code)
        process = self._process_for(receipt)
        if process is None:
            return ProcessObservation(status="unknown")
        code = process.poll()
        if code is None:
            self._write_progress_heartbeat(receipt)
            return ProcessObservation(status="running")
        return ProcessObservation(status="exited", exit_code=code)

    async def wait(self, receipt: CursorProcessReceipt) -> HostTerminalResult:
        self.authenticate(receipt)
        durable = self.read_durable_terminal(receipt)
        if durable is not None:
            return durable
        process = self._require_process(receipt)
        started = receipt.started_at
        stop_heartbeat = threading.Event()

        def _refresh_heartbeats() -> None:
            while not stop_heartbeat.wait(_HEARTBEAT_REFRESH_SECONDS):
                if process.poll() is not None:
                    return
                self._write_progress_heartbeat(receipt)

        heartbeat_thread = threading.Thread(target=_refresh_heartbeats, daemon=True)
        heartbeat_thread.start()
        try:
            stdout, stderr, exit_code = await asyncio.to_thread(
                _collect_process_output_bounded,
                process,
                max_bytes=_MAX_CAPTURE_BYTES,
            )
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1.0)
            self._write_progress_heartbeat(receipt)
        elapsed = max(0.0, time.time() - started)
        terminal = HostTerminalResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=elapsed,
        )
        self._write_durable_terminal(receipt, terminal)
        self._cleanup_process(receipt.process_start_token)
        return terminal

    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None:
        self.authenticate(receipt)
        record = self._load_spawn_record(receipt.process_start_token)
        if record is None:
            raise TaskActivityProtocolViolation("process receipt does not match this host")
        root_pid = int(record["pid"])
        process_group = int(record["process_group"])
        await self._signal_targets(_collect_process_tree(root_pid), signal.SIGTERM, policy.graceful_seconds)
        await self._signal_targets(_collect_process_tree(root_pid), signal.SIGKILL, policy.forced_seconds)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process_group, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(-process_group, sig)
            except (ProcessLookupError, PermissionError):
                pass
        self._close_tracked_handles(receipt.process_start_token)

    async def _signal_targets(
        self, targets: tuple[int, ...], sig: signal.Signals, grace_seconds: float
    ) -> None:
        for pid in targets:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
        if not targets:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _any_process_alive(targets):
                return
            await asyncio.sleep(_TERMINATE_POLL_SECONDS)

    def _confinement_identity(self, process_group: int) -> str:
        return f"pgid:{process_group}"

    def _spawn_path(self, token: str) -> Path:
        return self._host_dir / _SPAWNS_DIR_NAME / f"{token}.json"

    def _terminal_path(self, token: str) -> Path:
        return self._host_dir / _TERMINALS_DIR_NAME / f"{token}.json"

    def _heartbeat_path(self, token: str) -> Path:
        return self._host_dir / _HEARTBEATS_DIR_NAME / f"{token}.json"

    def _write_progress_heartbeat(self, receipt: CursorProcessReceipt) -> float:
        updated_at = time.time()
        _write_json_atomically(
            self._heartbeat_path(receipt.process_start_token),
            {
                "updated_at": updated_at,
                "receipt_mac": _heartbeat_mac(self._mac_key, receipt, updated_at),
                "host_instance_id": self._host_instance_id,
            },
        )
        return updated_at

    def _persist_spawn_record(
        self,
        *,
        receipt: CursorProcessReceipt,
        pid: int,
        process_group: int,
        start_identity: str,
        receipt_mac: str,
    ) -> None:
        _write_json_atomically(
            self._spawn_path(receipt.process_start_token),
            {
                "receipt": receipt.model_dump(mode="json"),
                "pid": pid,
                "process_group": process_group,
                "start_identity": start_identity,
                "receipt_mac": receipt_mac,
                "host_instance_id": self._host_instance_id,
                "request_digest": receipt.request_digest,
            },
        )

    def _load_spawn_record(self, token: str) -> dict[str, object] | None:
        path = self._spawn_path(token)
        if not path.is_file():
            return None
        return _read_json(path)

    def _spawn_record_for_request(self, request_digest: str) -> dict[str, object] | None:
        spawns_dir = self._host_dir / _SPAWNS_DIR_NAME
        if not spawns_dir.is_dir():
            return None
        for path in spawns_dir.glob("*.json"):
            record = _read_json(path)
            if record.get("request_digest") == request_digest:
                return record
        return None

    def _write_durable_terminal(self, receipt: CursorProcessReceipt, terminal: HostTerminalResult) -> None:
        payload = {
            "terminal": {
                "exit_code": terminal.exit_code,
                "stdout": terminal.stdout.hex(),
                "stderr": terminal.stderr.hex(),
                "elapsed_seconds": terminal.elapsed_seconds,
            },
            "receipt_mac": _terminal_mac(self._mac_key, receipt, terminal),
        }
        _write_json_atomically(self._terminal_path(receipt.process_start_token), payload)

    def _start_identity_matches(self, record: Mapping[str, object]) -> bool:
        receipt_payload = record.get("receipt")
        if isinstance(receipt_payload, dict):
            token = receipt_payload.get("process_start_token")
            if isinstance(token, str) and self._terminal_path(token).is_file():
                return True
        pid = record.get("pid")
        start_identity = record.get("start_identity")
        if not isinstance(pid, int) or not isinstance(start_identity, str):
            return False
        try:
            return self._read_start_identity(pid) == start_identity
        except TaskActivityProtocolViolation:
            return False

    def _track_process(self, token: str, process: subprocess.Popen[bytes]) -> None:
        self._processes[token] = process

    def _process_for(self, receipt: CursorProcessReceipt) -> subprocess.Popen[bytes] | None:
        return self._processes.get(receipt.process_start_token)

    def _require_process(self, receipt: CursorProcessReceipt) -> subprocess.Popen[bytes]:
        process = self._process_for(receipt)
        if process is None:
            record = self._load_spawn_record(receipt.process_start_token)
            if record is None:
                raise TaskActivityProtocolViolation("process receipt does not match this host")
            raise TaskActivityProtocolViolation("process is not attached to this host instance")
        return process

    def _cleanup_process(self, token: str) -> None:
        self._reap_tracked_process(token)

    def _close_tracked_handles(self, token: str) -> None:
        process = self._processes.get(token)
        if process is None:
            return
        _close_process_handles(process)

    def _reap_tracked_process(self, token: str) -> None:
        process = self._processes.pop(token, None)
        if process is None:
            return
        if process.poll() is None:
            for pid in _collect_process_tree(process.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    continue
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=1.0)
        _close_process_handles(process)

    def _read_start_identity(self, pid: int) -> str:
        raise NotImplementedError


class MacOSProcessGroupHost(_BaseProductionConfinedProcessHost):
    def _read_start_identity(self, pid: int) -> str:
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise TaskActivityProtocolViolation("process start identity is unavailable") from error
        start = completed.stdout.strip()
        if not start:
            raise TaskActivityProtocolViolation("process start identity is unavailable")
        return f"proc:{pid}:{start}"


class LinuxProcessSupervisorHost(_BaseProductionConfinedProcessHost):
    _confinement_mechanism = "container"

    def _confinement_identity(self, process_group: int) -> str:
        return f"session:{process_group}"

    def _read_proc_stat(self, pid: int) -> str:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read()

    def _read_start_identity(self, pid: int) -> str:
        try:
            stat = self._read_proc_stat(pid)
        except OSError as error:
            raise TaskActivityProtocolViolation("process start identity is unavailable") from error
        tail = stat.rpartition(") ")[2].split()
        if len(tail) < 20:
            raise TaskActivityProtocolViolation("process start identity is unavailable")
        starttime = tail[19]
        ppid = tail[1]
        return f"proc:{pid}:{starttime}:{ppid}"


__all__ = [
    "CancelPolicy",
    "ConfinedProcess",
    "ConfinedProcessHost",
    "ConfinementIdentity",
    "CursorProcessReceipt",
    "HostTerminalResult",
    "LinuxProcessSupervisorHost",
    "MacOSProcessGroupHost",
    "ProcessLaunchRequest",
    "ProcessObservation",
    "UnsupportedCursorPlatform",
    "authenticate_confinement",
    "authenticate_executable",
    "argv_policy_document",
    "build_launch_request",
    "cursor_dispatch_fingerprint",
    "production_process_host",
    "workspace_identity_digest_for",
]
