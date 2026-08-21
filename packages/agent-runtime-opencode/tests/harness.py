from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_runtime_contracts import (
    AgentRunRequest,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.discovery import discovery_metadata, adapter_source_digest
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import OpenCodeProtocolProfile
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
)


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_SECRET_TEXT = "canary-secret-value"


class FakeActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self._snapshot = snapshot

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


class ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


@dataclass
class OpenCodeFixture:
    fake: OpenCodeFakeServer
    handler: OpenCodeHandler
    config: OpenCodeAdapterConfig
    request: TaskRequest
    context: TaskContext
    port: FakeActivityPort
    metadata: dict[str, object]
    _root: tempfile.TemporaryDirectory[str]

    @property
    def activity(self) -> TaskActivitySnapshot:
        return self.port.snapshot

    async def execute_until_cut(self) -> None:
        try:
            await self.handler.execute(self.request, self.context)
        except Exception:
            return

    async def reconcile_twice(self) -> None:
        await self.handler.reconcile(self.request, self.context, self.activity)
        await self.handler.reconcile(self.request, self.context, self.activity)

    def close(self) -> None:
        self.fake.close()
        self._root.cleanup()


def profile(**overrides: object) -> OpenCodeProtocolProfile:
    payload: dict[str, object] = {
        "prompt_idempotency": "conflict-on-body-drift",
        "metadata_supported": True,
        "sse_supported": True,
        "poll_fallback_supported": True,
    }
    payload.update(overrides)
    return OpenCodeProtocolProfile.model_validate(payload)


def agent_run_request() -> AgentRunRequest:
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=_SHA,
            extraction_mode="structured",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest=_SHA,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        request_policy_digest=_SHA,
        request_config_digest=_SHA,
    )


def task_request(agent_run: AgentRunRequest | None = None, **overrides: object) -> TaskRequest:
    payload = agent_run if agent_run is not None else agent_run_request()
    fields: dict[str, object] = {
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "graph_instance_id": "graph-1",
        "node_id": "run",
        "capability_id": "runtime.opencode.execute",
        "invocation": InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        "attempt": 1,
        "input": payload.model_dump(mode="json"),
    }
    fields.update(overrides)
    return TaskRequest.model_validate(fields)


def workspace_identity() -> AttemptWorkspaceIdentity:
    return AttemptWorkspaceIdentity(
        attempt_directory_id="attempt-1",
        baseline_tree_id=_SHA,
        attempt_identity_digest="b" * 64,
    )


def prepared_snapshot(request: TaskRequest) -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest=canonical_digest(request.model_dump(mode="json")),
        workspace_identity=workspace_identity(),
        state="prepared",
    )


def _config(fake: OpenCodeFakeServer, **overrides: object) -> OpenCodeAdapterConfig:
    payload: dict[str, object] = {
        "schema_version": "1",
        "endpoint": fake.base_url,
        "tls_identity_digest": _SHA,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": fake.project_scope,
        "request_timeout_seconds": 5,
        "observation_horizon_seconds": 30,
        "max_response_bytes": 65536,
    }
    payload.update(overrides)
    return OpenCodeAdapterConfig.model_validate(payload)


def _open_code_fixture(
    *,
    create_cut: str | None = None,
    existing_matches: int = 0,
    hide_sessions: bool = False,
    secrets: SecretPort | None = None,
) -> OpenCodeFixture:
    root = tempfile.TemporaryDirectory()
    workspace_root = Path(root.name) / "attempt-1"
    workspace_root.mkdir()
    request = task_request()
    snapshot = prepared_snapshot(request)
    metadata = discovery_metadata(
        request=request,
        snapshot=snapshot,
        adapter_source_digest=adapter_source_digest(),
    )
    fake = OpenCodeFakeServer(
        profile=profile(),
        create_cut=create_cut,  # type: ignore[arg-type]
        existing_matches=existing_matches,
        metadata=metadata.model_dump(mode="json"),
        hide_sessions=hide_sessions,
    )
    port = FakeActivityPort(snapshot)
    config = _config(fake)
    context = TaskContext(
        workspace_root=workspace_root,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=request.invocation,
        activity=port,
        secrets=secrets if secrets is not None else ExactSecretPort({"opencode.token": _CANARY}),
    )
    return OpenCodeFixture(
        fake=fake,
        handler=OpenCodeHandler(config),
        config=config,
        request=request,
        context=context,
        port=port,
        metadata=metadata.model_dump(mode="json"),
        _root=root,
    )


def metadata_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "activation_id": "a1",
        "attempt": 1,
        "activity_id": "activity-1",
        "request_digest": _SHA,
        "workspace_identity_digest": _SHA,
        "adapter_source_digest": _SHA,
    }
    payload.update(overrides)
    return payload


def reference_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "profile_identity_digest": _SHA,
        "session_id": "ses_generated_1",
        "metadata_match_digest": _SHA,
        "request_digest": _SHA,
        "expected_message_id": _SHA,
        "prompt_body_digest": _SHA,
        "adapter_version": "0.1.0",
    }
    payload.update(overrides)
    return payload
