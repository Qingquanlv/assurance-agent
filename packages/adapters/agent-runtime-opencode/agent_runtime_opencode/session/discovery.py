from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_serializer

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import canonical_digest, reject_credentials_in_digest_input, thaw_json
from graph_engine.plugin_api import (
    FrozenModel,
    TaskActivityPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskRequest,
)

from agent_runtime_opencode.transport.http import OpenCodeHttpClient


ADAPTER_VERSION = "0.2.0"


class OpenCodeDispatchIncomplete(Exception):
    """Raised when execute cannot bind before prompt admission."""


class OpenCodeDiscoveryMetadata(FrozenModel):
    schema_version: Literal["1"] = "1"
    invocation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    activation_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    activity_id: str = Field(min_length=1)
    request_digest: str = Field(min_length=64, max_length=64)
    workspace_identity_digest: str = Field(min_length=64, max_length=64)
    adapter_source_digest: str = Field(min_length=64, max_length=64)


class OpenCodeActivityReference(FrozenModel):
    profile_identity_digest: str = Field(min_length=64, max_length=64)
    session_id: str | None = None
    parent_session_id: str | None = None
    worktree: str | None = None
    metadata_match_digest: str = Field(min_length=64, max_length=64)
    request_digest: str = Field(min_length=64, max_length=64)
    expected_message_id: str = Field(min_length=1)
    prompt_body_digest: str = Field(min_length=64, max_length=64)
    adapter_version: str = Field(min_length=1)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: object) -> object:
        serialized = handler(self)  # type: ignore[operator]
        if isinstance(serialized, dict):
            for key in ("parent_session_id", "worktree"):
                if serialized.get(key) is None:
                    serialized.pop(key, None)
        return serialized


class OpenCodeSessionMetadata(FrozenModel):
    discovery: OpenCodeDiscoveryMetadata
    workspace_binding: dict[str, JsonValue]


class OpenCodeSessionCreateRequest(FrozenModel):
    title: str = Field(min_length=1)
    metadata: OpenCodeSessionMetadata
    agent: str | None = None
    parentID: str | None = None


def discovery_metadata_from_record(record: Mapping[str, object]) -> OpenCodeDiscoveryMetadata:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("foreign session metadata")
    return OpenCodeDiscoveryMetadata.model_validate(metadata.get("discovery"))


def adapter_source_digest() -> str:
    return canonical_digest(
        {
            "distribution": "agent-runtime-opencode",
            "entrypoint": "agent_runtime_opencode.plugin:OpenCodePlugin",
            "version": ADAPTER_VERSION,
        }
    )


def activation_identity(request: TaskRequest) -> str:
    return canonical_digest(
        {
            "graph_instance_id": request.graph_instance_id,
            "node_id": request.node_id,
        }
    )


def discovery_metadata(
    *,
    request: TaskRequest,
    snapshot: TaskActivitySnapshot,
    adapter_source_digest: str,
) -> OpenCodeDiscoveryMetadata:
    return OpenCodeDiscoveryMetadata(
        invocation_id=request.invocation_id,
        task_id=request.task_id,
        activation_id=activation_identity(request),
        attempt=request.attempt,
        activity_id=snapshot.activity_id,
        request_digest=snapshot.request_digest,
        workspace_identity_digest=canonical_digest(snapshot.workspace_identity.model_dump(mode="json")),
        adapter_source_digest=adapter_source_digest,
    )


def expected_message_id(snapshot: TaskActivitySnapshot) -> str:
    digest = canonical_digest(
        {
            "activity_id": snapshot.activity_id,
            "kind": "opencode-prompt-message",
            "request_digest": snapshot.request_digest,
        }
    )
    return f"msg_{digest}"


def prompt_body_digest(agent_run: AgentRunRequest) -> str:
    return canonical_digest(agent_run.model_dump(mode="json"))


def metadata_match_digest(metadata: OpenCodeDiscoveryMetadata) -> str:
    return canonical_digest(metadata.model_dump(mode="json"))


def exact_metadata_matches(
    sessions: Sequence[object],
    expected: OpenCodeDiscoveryMetadata,
    *,
    parent_session_id: str | None = None,
    worktree: str | None = None,
) -> tuple[Mapping[str, object], ...]:
    expected_digest = metadata_match_digest(expected)
    matches: list[Mapping[str, object]] = []
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        parent = session.get("parentID")
        if parent_session_id is None:
            if isinstance(parent, str) and parent:
                continue
        elif parent != parent_session_id:
            continue
        if worktree is not None:
            directory = session.get("directory")
            if (
                not isinstance(directory, str)
                or not directory
                or Path(directory).resolve() != Path(worktree).resolve()
            ):
                continue
        try:
            metadata = discovery_metadata_from_record(session)
        except (TypeError, ValueError):
            continue
        if metadata_match_digest(metadata) == expected_digest:
            matches.append(session)
    return tuple(matches)


def agent_run_from_request(request: TaskRequest) -> AgentRunRequest:
    return AgentRunRequest.model_validate(thaw_json(request.input))


async def _list_sessions(client: OpenCodeHttpClient) -> list[object]:
    raw = await client.list_sessions()
    if not isinstance(raw, list):
        raise ValueError("session list shape must be a JSON array")
    return raw


def _bind_match(
    port: TaskActivityPort,
    record: object,
    expected: dict[str, str],
) -> TaskActivityReconcileResult:
    if not isinstance(record, dict):
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="session record is not an object",
        )
    session_id = record.get("id")
    if not isinstance(session_id, str) or not session_id:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="generated session id is missing",
        )
    reference = OpenCodeActivityReference(session_id=session_id, **expected)
    dumped = reference.model_dump(mode="json")
    reject_credentials_in_digest_input(dumped)
    snapshot = port.bind(dumped)
    return TaskActivityReconcileResult(status="running", reference=thaw_json(snapshot.reference))


__all__ = [
    "ADAPTER_VERSION",
    "OpenCodeActivityReference",
    "OpenCodeDiscoveryMetadata",
    "OpenCodeDispatchIncomplete",
    "OpenCodeSessionCreateRequest",
    "OpenCodeSessionMetadata",
    "activation_identity",
    "adapter_source_digest",
    "agent_run_from_request",
    "discovery_metadata",
    "discovery_metadata_from_record",
    "exact_metadata_matches",
    "expected_message_id",
    "metadata_match_digest",
    "prompt_body_digest",
]
