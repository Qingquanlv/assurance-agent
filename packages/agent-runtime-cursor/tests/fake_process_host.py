from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Literal

from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_cursor.process import (
    CancelPolicy,
    ConfinedProcess,
    ConfinementIdentity,
    CursorProcessReceipt,
    HostTerminalResult,
    ProcessLaunchRequest,
    ProcessObservation,
)
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import TaskActivitySnapshot

BOOT_DIGEST = "b" * 64
_ACCEPTABLE_MECHANISMS = frozenset({"process-group", "job-object", "cgroup", "container"})


class ProcessDispatchCut(RuntimeError):
    """Raised by the fake host at an injected crash cut."""

    def __init__(self, cut: str) -> None:
        super().__init__(cut)
        self.cut = cut


class FakeActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self._snapshot = snapshot
        self.bind_calls: list[object] = []

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._snapshot

    def mark_dispatch_started(self, fingerprint: object) -> TaskActivitySnapshot:
        digest = canonical_digest(fingerprint)
        current = self._snapshot.dispatch_fingerprint_digest
        if current is not None:
            if current != digest:
                raise ValueError("dispatch fingerprint drifted from the durable activity")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "dispatch_started",
                "dispatch_fingerprint": fingerprint,
                "dispatch_fingerprint_digest": digest,
            }
        )
        return self._snapshot

    def bind(self, reference: object) -> TaskActivitySnapshot:
        self.bind_calls.append(reference)
        digest = canonical_digest(reference)
        current = self._snapshot.reference_digest
        if current is not None:
            if current != digest:
                raise ValueError("activity reference changed after bind")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "bound",
                "reference": reference,
                "reference_digest": digest,
            }
        )
        return self._snapshot

    def replace_bound_reference(self, reference: object) -> None:
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "bound",
                "reference": reference,
                "reference_digest": canonical_digest(reference),
            }
        )


class FakeConfinedProcessHost:
    def __init__(
        self,
        *,
        available: bool = True,
        alive: bool = True,
        mechanism: str = "process-group",
        descendant_inheritance: bool = True,
        reported_version: str = "1.0.0",
        status: str = "exited",
        exit_code: int | None = 0,
        stdout: bytes | None = None,
        stderr: bytes = b"",
        elapsed_seconds: float = 0.1,
        boot_identity_digest: str = BOOT_DIGEST,
        process_start_token: str = "start-token-1",
        host_instance_id: str | None = None,
        cut: str | None = None,
        cleanup_mode: Literal["success", "ambiguous", "leave_running"] = "success",
        reject_spawns: bool = False,
        wait_hook: Callable[[CursorProcessReceipt], None] | None = None,
    ) -> None:
        self.available = available
        self.alive = alive
        self.mechanism = mechanism
        self.descendant_inheritance = descendant_inheritance
        self.reported_version = reported_version
        self.status = status
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed_seconds = elapsed_seconds
        self.boot_identity_digest = boot_identity_digest
        self.process_start_token = process_start_token
        self.host_instance_id = host_instance_id if host_instance_id is not None else uuid.uuid4().hex
        self.cut = cut
        self.cleanup_mode = cleanup_mode
        self.reject_spawns = reject_spawns
        self.wait_hook = wait_hook
        self.launches: list[ProcessLaunchRequest] = []
        self.wait_calls: list[CursorProcessReceipt] = []
        self.terminations: list[tuple[CursorProcessReceipt, CancelPolicy]] = []
        self.signals: list[str] = []
        self.spawn_count = 0
        self.session_adoptions: list[str] = []
        self._receipt: CursorProcessReceipt | None = None
        self._durable_terminal: HostTerminalResult | None = None
        self._cut_consumed = False

    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity:
        if not self.available:
            raise TaskActivityProtocolViolation("confinement is unavailable")
        if request.shell:
            raise TaskActivityProtocolViolation("confinement forbids shell execution")
        return ConfinementIdentity(
            mechanism=self.mechanism,
            identity="pgid:4242" if self.mechanism in _ACCEPTABLE_MECHANISMS else "pid:1",
            descendant_inheritance=self.descendant_inheritance,
            host_boot_identity_digest=self.boot_identity_digest,
            host_instance_id=self.host_instance_id,
            executable_version=self.reported_version,
        )

    def unbound_spawn_state(
        self, fingerprint: Mapping[str, object]
    ) -> Literal["not_spawned", "spawned", "unknown"]:
        if not self.alive:
            return "unknown"
        if (
            fingerprint.get("host_instance_id") != self.host_instance_id
            or fingerprint.get("host_boot_identity_digest") != self.boot_identity_digest
        ):
            return "unknown"
        if self.spawn_count > 0 or self._receipt is not None:
            return "spawned"
        return "not_spawned"

    def read_durable_terminal(self, receipt: CursorProcessReceipt) -> HostTerminalResult | None:
        self.authenticate(receipt)
        return self._durable_terminal

    def reuse_pid(self) -> None:
        if self._receipt is None:
            raise TaskActivityProtocolViolation("no child exists to reuse")
        self.process_start_token = "start-token-reused"
        self._receipt = self._receipt.model_copy(update={"process_start_token": self.process_start_token})

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess:
        if self.cut == "before_spawn":
            raise ProcessDispatchCut("before_spawn")
        if self.reject_spawns:
            raise TaskActivityProtocolViolation("cursor access is forbidden")
        identity = self.preflight(request)
        self.launches.append(request)
        receipt = CursorProcessReceipt(
            host_boot_identity_digest=identity.host_boot_identity_digest,
            host_instance_id=identity.host_instance_id,
            confinement_identity=identity.identity,
            process_group_identity=identity.identity,
            process_start_token=self.process_start_token,
            executable_version_digest=request.executable_version_digest,
            request_digest=request.request_digest,
            argv_policy_digest=request.argv_policy_digest,
            workspace_identity_digest=request.workspace_identity_digest,
            started_at=0.0,
        )
        self._receipt = receipt
        self.spawn_count += 1
        if self.status == "running":
            self.exit_code = None
        if self.cut == "after_spawn_before_bind":
            raise ProcessDispatchCut("after_spawn_before_bind")
        return ConfinedProcess(receipt=receipt)

    def authenticate(self, receipt: CursorProcessReceipt) -> None:
        if not self.alive:
            raise TaskActivityProtocolViolation("host is not live")
        if not self.available:
            raise TaskActivityProtocolViolation("confinement is unavailable")
        if (
            receipt.host_boot_identity_digest != self.boot_identity_digest
            or receipt.host_instance_id != self.host_instance_id
            or self._receipt is None
            or receipt.process_start_token != self._receipt.process_start_token
            or receipt.confinement_identity != self._receipt.confinement_identity
            or receipt.process_group_identity != self._receipt.process_group_identity
        ):
            raise TaskActivityProtocolViolation("process receipt does not match this host")

    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation:
        self.authenticate(receipt)
        status = self.status
        if status not in {"running", "exited", "unknown"}:
            status = "unknown"
        return ProcessObservation(status=status, exit_code=self.exit_code)  # type: ignore[arg-type]

    async def wait(self, receipt: CursorProcessReceipt) -> HostTerminalResult:
        self.authenticate(receipt)
        self.wait_calls.append(receipt)
        if self.wait_hook is not None:
            self.wait_hook(receipt)
        if self.cut == "after_bind" and not self._cut_consumed:
            self._cut_consumed = True
            raise ProcessDispatchCut("after_bind")
        if self.status != "exited" or self.exit_code is None:
            raise TaskActivityProtocolViolation("process has not exited")
        stdout = self.stdout if self.stdout is not None else _default_stdout(self.launches[-1])
        result = HostTerminalResult(
            exit_code=self.exit_code,
            stdout=stdout,
            stderr=self.stderr,
            elapsed_seconds=self.elapsed_seconds,
        )
        if self.cut == "mid_stream" and not self._cut_consumed:
            self._cut_consumed = True
            truncated = HostTerminalResult(
                exit_code=self.exit_code,
                stdout=stdout.split(b"\n", 1)[0] + b"\n" + b'{"type":"assistant","session_id":"sess-1"',
                stderr=self.stderr,
                elapsed_seconds=self.elapsed_seconds,
            )
            self._durable_terminal = truncated
            raise ProcessDispatchCut("mid_stream")
        if self.cut == "after_host_terminal_receipt" and not self._cut_consumed:
            self._cut_consumed = True
            self._durable_terminal = result
            raise ProcessDispatchCut("after_host_terminal_receipt")
        if self._durable_terminal is not None:
            return self._durable_terminal
        self._durable_terminal = result
        return result

    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None:
        self.authenticate(receipt)
        self.terminations.append((receipt, policy))
        self.signals.append("graceful")
        if self.cleanup_mode == "leave_running":
            return
        if self.status == "running":
            self.signals.append("forced")
        if self.cleanup_mode == "ambiguous":
            self.status = "unknown"
            self.exit_code = None
            return
        self.status = "exited"
        if self.exit_code is None:
            self.exit_code = 1


def _default_stdout(launch: ProcessLaunchRequest) -> bytes:
    import json

    cwd = str(launch.cwd)
    init = {"type": "system", "subtype": "init", "cwd": cwd, "session_id": "sess-1"}
    terminal = {
        "is_error": False,
        "result": {"ok": True},
        "session_id": "sess-1",
        "subtype": "success",
        "type": "result",
    }
    return (json.dumps(init) + "\n" + json.dumps(terminal) + "\n").encode("utf-8")
