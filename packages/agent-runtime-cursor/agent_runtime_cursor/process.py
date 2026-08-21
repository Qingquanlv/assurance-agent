from __future__ import annotations

import hashlib
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

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


class ConfinementIdentity(FrozenModel):
    mechanism: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    descendant_inheritance: bool
    host_boot_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    executable_version: str = Field(min_length=1)


class CursorProcessReceipt(FrozenModel):
    host_boot_identity_digest: str = Field(pattern=_SHA256_PATTERN)
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


class ConfinedProcessHost(Protocol):
    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity: ...

    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess: ...

    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation: ...

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


def cursor_dispatch_fingerprint(
    config: CursorAdapterConfig,
    argv: tuple[str, ...],
) -> dict[str, object]:
    fingerprint: dict[str, object] = {
        "protocol_profile": PROTOCOL_PROFILE,
        "executable_digest": config.executable_digest,
        "executable_version_digest": canonical_digest(config.expected_version),
        "argv_policy_digest": canonical_digest(argv_policy_document(argv, config.environment_names)),
    }
    reject_credentials_in_digest_input(fingerprint)
    return fingerprint


def _resolve_environment(
    config: CursorAdapterConfig, *, executable: Path, context: TaskContext
) -> Mapping[str, str]:
    resolved: dict[str, str] = {}
    for name in config.environment_names:
        if name == "PATH":
            resolved[name] = str(executable.parent)
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
    return ProcessLaunchRequest(
        argv=argv,
        cwd=context.workspace_root.resolve(),
        environment=_resolve_environment(config, executable=executable, context=context),
        stdin=canonical_json_bytes(agent_run.model_dump(mode="json")),
        shell=False,
        executable_version_digest=canonical_digest(config.expected_version),
        request_digest=canonical_digest(agent_run.model_dump(mode="json")),
        argv_policy_digest=canonical_digest(policy),
        workspace_identity_digest=canonical_digest({"cwd_policy": "attempt-workspace"}),
    )
