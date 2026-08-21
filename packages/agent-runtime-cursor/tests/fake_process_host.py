from __future__ import annotations

from agent_runtime_cursor.process import (
    CancelPolicy,
    ConfinedProcess,
    ConfinementIdentity,
    CursorProcessReceipt,
    ProcessLaunchRequest,
    ProcessObservation,
)
from graph_engine import TaskActivityProtocolViolation

_BOOT_DIGEST = "b" * 64
_ACCEPTABLE_MECHANISMS = frozenset({"process-group", "job-object", "cgroup", "container"})


class FakeConfinedProcessHost:
    def __init__(
        self,
        *,
        available: bool = True,
        mechanism: str = "process-group",
        descendant_inheritance: bool = True,
        reported_version: str = "1.0.0",
    ) -> None:
        self.available = available
        self.mechanism = mechanism
        self.descendant_inheritance = descendant_inheritance
        self.reported_version = reported_version
        self.launches: list[ProcessLaunchRequest] = []

    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity:
        if not self.available:
            raise TaskActivityProtocolViolation("confinement is unavailable")
        if request.shell:
            raise TaskActivityProtocolViolation("confinement forbids shell execution")
        return ConfinementIdentity(
            mechanism=self.mechanism,
            identity="pgid:4242" if self.mechanism in _ACCEPTABLE_MECHANISMS else "pid:1",
            descendant_inheritance=self.descendant_inheritance,
            host_boot_identity_digest=_BOOT_DIGEST,
            executable_version=self.reported_version,
        )

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess:
        identity = self.preflight(request)
        self.launches.append(request)
        receipt = CursorProcessReceipt(
            host_boot_identity_digest=identity.host_boot_identity_digest,
            confinement_identity=identity.identity,
            process_group_identity=identity.identity,
            process_start_token="start-token-1",
            executable_version_digest=request.executable_version_digest,
            request_digest=request.request_digest,
            argv_policy_digest=request.argv_policy_digest,
            workspace_identity_digest=request.workspace_identity_digest,
            started_at=0.0,
        )
        return ConfinedProcess(receipt=receipt)

    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation:
        del receipt
        return ProcessObservation(status="running")

    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None:
        del receipt, policy
