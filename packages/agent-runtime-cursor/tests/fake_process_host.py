from __future__ import annotations

from collections.abc import Callable

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


class FakeConfinedProcessHost:
    def __init__(
        self,
        *,
        available: bool = True,
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
        wait_hook: Callable[[CursorProcessReceipt], None] | None = None,
    ) -> None:
        self.available = available
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
        self.wait_hook = wait_hook
        self.launches: list[ProcessLaunchRequest] = []
        self.wait_calls: list[CursorProcessReceipt] = []
        self.terminations: list[tuple[CursorProcessReceipt, CancelPolicy]] = []
        self.signals: list[str] = []
        self._receipt: CursorProcessReceipt | None = None

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
            executable_version=self.reported_version,
        )

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess:
        identity = self.preflight(request)
        self.launches.append(request)
        receipt = CursorProcessReceipt(
            host_boot_identity_digest=identity.host_boot_identity_digest,
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
        if self.status == "running":
            self.exit_code = None
        return ConfinedProcess(receipt=receipt)

    def authenticate(self, receipt: CursorProcessReceipt) -> None:
        if (
            receipt.host_boot_identity_digest != self.boot_identity_digest
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
        stdout = self.stdout if self.stdout is not None else _default_stdout(self.launches[-1])
        return HostTerminalResult(
            exit_code=0 if self.exit_code is None else self.exit_code,
            stdout=stdout,
            stderr=self.stderr,
            elapsed_seconds=self.elapsed_seconds,
        )

    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None:
        self.authenticate(receipt)
        self.terminations.append((receipt, policy))
        self.signals.append("graceful")
        if self.status == "running":
            self.signals.append("forced")
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
