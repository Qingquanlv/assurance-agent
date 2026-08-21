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
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.discovery import (
    ADAPTER_VERSION,
    OpenCodeActivityReference,
    adapter_source_digest,
    agent_run_from_request,
    discovery_metadata,
    expected_message_id,
    metadata_match_digest,
    prompt_body_digest,
)
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import OpenCodeProtocolProfile, canonical_json_text
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
    heartbeats: list[int]
    _root: tempfile.TemporaryDirectory[str]

    @property
    def activity(self) -> TaskActivitySnapshot:
        return self.port.snapshot

    @property
    def reference(self) -> OpenCodeActivityReference:
        return OpenCodeActivityReference.model_validate(self.port.snapshot.reference)

    async def execute_until_cut(self) -> None:
        try:
            await self.handler.execute(self.request, self.context)
        except Exception:
            return

    async def reconcile(self) -> object:
        return await self.handler.reconcile(self.request, self.context, self.activity)

    async def reconcile_twice(self) -> None:
        await self.handler.reconcile(self.request, self.context, self.activity)
        await self.handler.reconcile(self.request, self.context, self.activity)

    async def run_and_reconcile(self) -> None:
        await self.execute_until_cut()
        await self.reconcile_twice()

    def bind_session(self, session_id: str) -> None:
        fingerprint = _fingerprint(self.config, self.fake.profile)
        self.port.mark_dispatch_started(fingerprint)
        metadata = discovery_metadata(
            request=self.request,
            snapshot=self.port.snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        agent_run = agent_run_from_request(self.request)
        reference = OpenCodeActivityReference(
            session_id=session_id,
            profile_identity_digest=canonical_digest(fingerprint),
            metadata_match_digest=metadata_match_digest(metadata),
            request_digest=self.port.snapshot.request_digest,
            expected_message_id=expected_message_id(self.port.snapshot),
            prompt_body_digest=prompt_body_digest(agent_run),
            adapter_version=ADAPTER_VERSION,
        )
        self.port.bind(reference.model_dump(mode="json"))

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
    prompt_cut: str | None = None,
    config_overrides: dict[str, object] | None = None,
    profile_overrides: dict[str, object] | None = None,
    agent_run: AgentRunRequest | None = None,
) -> OpenCodeFixture:
    root = tempfile.TemporaryDirectory()
    workspace_root = Path(root.name) / "attempt-1"
    workspace_root.mkdir()
    request = task_request(agent_run)
    snapshot = prepared_snapshot(request)
    metadata = discovery_metadata(
        request=request,
        snapshot=snapshot,
        adapter_source_digest=adapter_source_digest(),
    )
    fake = OpenCodeFakeServer(
        profile=profile(**(profile_overrides or {})),
        create_cut=create_cut,  # type: ignore[arg-type]
        existing_matches=existing_matches,
        metadata=metadata.model_dump(mode="json"),
        hide_sessions=hide_sessions,
        prompt_cut=prompt_cut,  # type: ignore[arg-type]
    )
    port = FakeActivityPort(snapshot)
    config = _config(fake, **(config_overrides or {}))
    heartbeats: list[int] = []

    def _heartbeat() -> None:
        heartbeats.append(1)

    context = TaskContext(
        workspace_root=workspace_root,
        heartbeat=_heartbeat,
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
        heartbeats=heartbeats,
        _root=root,
    )


def _bound_fixture(
    *,
    prompt_cut: str | None = None,
    existing_message_body: dict[str, object] | None = None,
    terminal_mode: str = "busy",
    sse_mode: str = "heartbeat",
    omit_status: bool = False,
    request_timeout_seconds: float | None = None,
    poll_fallback_supported: bool = True,
    agent_run: AgentRunRequest | None = None,
) -> OpenCodeFixture:
    overrides: dict[str, object] = {}
    if request_timeout_seconds is not None:
        overrides["request_timeout_seconds"] = request_timeout_seconds
    fixture = _open_code_fixture(
        prompt_cut=prompt_cut,
        config_overrides=overrides or None,
        profile_overrides={"poll_fallback_supported": poll_fallback_supported},
        agent_run=agent_run,
    )
    fixture.fake.terminal_mode = terminal_mode  # type: ignore[assignment]
    fixture.fake.sse_mode = sse_mode  # type: ignore[assignment]
    fixture.fake.omit_status = omit_status
    session = fixture.fake.add_session(metadata=fixture.metadata)
    session_id = str(session["id"])
    fixture.bind_session(session_id)
    if existing_message_body is not None:
        fixture.fake.plant_message(
            session_id,
            fixture.reference.expected_message_id,
            existing_message_body,
        )
    return fixture


def prompt_admission_body(request: TaskRequest, message_id: str) -> dict[str, object]:
    agent_run = agent_run_from_request(request)
    parts: list[dict[str, object]] = []
    for instruction in agent_run.instructions:
        if instruction.text_content is not None:
            parts.append({"type": "text", "text": instruction.text_content})
        else:
            parts.append(
                {
                    "type": "text",
                    "text": canonical_json_text(thaw_json(instruction.json_content)),
                }
            )
    body: dict[str, object] = {
        "messageID": message_id,
        "parts": parts,
        "agent": agent_run.execution.worker_profile,
    }
    model = agent_run.execution.provider_model
    if model != "provider_default":
        provider, separator, model_id = model.partition("/")
        body["model"] = {
            "providerID": provider,
            "modelID": model_id if separator else provider,
        }
    return body


def _fingerprint(config: OpenCodeAdapterConfig, profile: OpenCodeProtocolProfile) -> dict[str, object]:
    identity = {"healthy": True, "version": "opencode-http-v1"}
    return {
        "endpoint_origin": config.origin,
        "tls_identity_digest": config.tls_identity_digest,
        "protocol_profile": profile.protocol_profile,
        "prompt_admission": profile.prompt_admission,
        "prompt_idempotency": profile.prompt_idempotency,
        "server_identity_digest": canonical_digest(identity),
        "project_scope": config.project_scope,
    }


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
