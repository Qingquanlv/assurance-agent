from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import canonical_digest, reject_credentials_in_digest_input, thaw_json
from graph_engine.plugin_api import (
    SecretHandleUnauthorized,
    TaskActivityCancelResult,
    TaskActivityPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.discovery import (
    ADAPTER_VERSION,
    OpenCodeActivityReference,
    OpenCodeDiscoveryMetadata,
    OpenCodeDispatchIncomplete,
    OpenCodeSessionCreateRequest,
    adapter_source_digest,
    agent_run_from_request,
    discovery_metadata,
    exact_metadata_matches,
    expected_message_id,
    metadata_match_digest,
    prompt_body_digest,
)
from agent_runtime_opencode.protocol import (
    AcceptedOpenCodeProfile,
    OpenCodeHttpClient,
    canonical_json_text,
)


class OpenCodeHandler:
    def __init__(self, config: OpenCodeAdapterConfig | None = None) -> None:
        self._config = config

    async def preflight(self, request: TaskRequest, context: TaskContext) -> dict[str, Any]:
        del request
        config = self._require_config()
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        secret = context.secrets.resolve(config.secret_handle)
        client = OpenCodeHttpClient(config, secret=secret)
        try:
            return await self._observe_fingerprint(client, secret)
        finally:
            await client.aclose()

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        result = await self._reconcile_session(request, context, allow_create=True)
        if result.status != "running":
            raise OpenCodeDispatchIncomplete(result.reason or result.status)
        raise NotImplementedError("OpenCode prompt admission is not implemented")

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="activity snapshot does not match the live port",
            )
        return await self._reconcile_session(request, context, allow_create=False)

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="indeterminate", reason="OpenCode cancel is not implemented")

    async def _reconcile_session(
        self,
        request: TaskRequest,
        context: TaskContext,
        *,
        allow_create: bool,
    ) -> TaskActivityReconcileResult:
        config = self._require_config()
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        snapshot = port.snapshot
        mismatch = self._identity_mismatch(request, context, snapshot)
        if mismatch is not None:
            if allow_create:
                raise ValueError(mismatch)
            return TaskActivityReconcileResult(status="indeterminate", reason=mismatch)
        secret = context.secrets.resolve(config.secret_handle)
        client = OpenCodeHttpClient(config, secret=secret)
        try:
            context.heartbeat()
            fingerprint = await self._observe_fingerprint(client, secret)
            expected = self._expected_reference_fields(request, snapshot, fingerprint)
            if snapshot.reference is not None:
                return await self._reconcile_bound(client, snapshot, expected)
            if snapshot.state == "prepared" and not allow_create:
                return await self._reconcile_prepared(client, port, request, snapshot, fingerprint, expected)
            was_prepared = snapshot.state == "prepared"
            snapshot = port.mark_dispatch_started(fingerprint)
            expected = self._expected_reference_fields(request, snapshot, fingerprint)
            return await self._discover_or_create(
                client,
                port,
                request,
                snapshot,
                expected,
                allow_create=allow_create and was_prepared,
            )
        except (httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError) as error:
            if allow_create:
                raise
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason=str(error) or "provider observation is indeterminate",
            )
        finally:
            await client.aclose()

    async def _reconcile_prepared(
        self,
        client: OpenCodeHttpClient,
        port: TaskActivityPort,
        request: TaskRequest,
        snapshot: TaskActivitySnapshot,
        fingerprint: dict[str, Any],
        expected: dict[str, str],
    ) -> TaskActivityReconcileResult:
        sessions = await self._list_sessions(client)
        metadata = discovery_metadata(
            request=request,
            snapshot=snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        matches = exact_metadata_matches(sessions, metadata)
        if len(matches) > 1:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="multiple exact metadata matches",
            )
        if len(matches) == 1:
            port.mark_dispatch_started(fingerprint)
            return self._bind_match(port, matches[0], expected)
        return TaskActivityReconcileResult(status="not_dispatched")

    async def _discover_or_create(
        self,
        client: OpenCodeHttpClient,
        port: TaskActivityPort,
        request: TaskRequest,
        snapshot: TaskActivitySnapshot,
        expected: dict[str, str],
        *,
        allow_create: bool,
    ) -> TaskActivityReconcileResult:
        sessions = await self._list_sessions(client)
        metadata = discovery_metadata(
            request=request,
            snapshot=snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        matches = exact_metadata_matches(sessions, metadata)
        if len(matches) > 1:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="multiple exact metadata matches",
            )
        if len(matches) == 1:
            return self._bind_match(port, matches[0], expected)
        if allow_create:
            return await self._create_and_bind(client, port, metadata, expected)
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="pending observation after ambiguous create",
        )

    async def _create_and_bind(
        self,
        client: OpenCodeHttpClient,
        port: TaskActivityPort,
        metadata: OpenCodeDiscoveryMetadata,
        expected: dict[str, str],
    ) -> TaskActivityReconcileResult:
        body = OpenCodeSessionCreateRequest(title=f"aa:{metadata.activity_id}", metadata=metadata)
        payload = body.model_dump(mode="json")
        if "id" in payload or "parentID" in payload:
            raise ValueError("create must not supply a session id or parentID")
        record = await client.create_session(payload)
        return self._bind_match(port, record, expected)

    async def _reconcile_bound(
        self,
        client: OpenCodeHttpClient,
        snapshot: TaskActivitySnapshot,
        expected: dict[str, str],
    ) -> TaskActivityReconcileResult:
        try:
            reference = OpenCodeActivityReference.model_validate(thaw_json(snapshot.reference))
        except ValidationError:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="bound reference is not authentic",
            )
        if self._reference_drifted(reference, expected):
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="bound reference identity drifted",
            )
        if not reference.session_id:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="bound session identity is unknown",
            )
        try:
            record = await client.get_session(reference.session_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return TaskActivityReconcileResult(
                    status="indeterminate",
                    reason="formerly bound session is missing",
                )
            raise
        parent = record.get("parentID") if isinstance(record, dict) else None
        if isinstance(parent, str) and parent:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="parentID reconnect is forbidden",
            )
        if not isinstance(record, dict) or record.get("id") != reference.session_id:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="session identity drifted",
            )
        try:
            observed = OpenCodeDiscoveryMetadata.model_validate(record.get("metadata"))
        except ValidationError:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="foreign session metadata",
            )
        if metadata_match_digest(observed) != reference.metadata_match_digest:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="foreign session metadata",
            )
        return TaskActivityReconcileResult(status="running", reference=thaw_json(snapshot.reference))

    def _bind_match(
        self,
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

    async def _list_sessions(self, client: OpenCodeHttpClient) -> list[object]:
        raw = await client.list_sessions()
        if not isinstance(raw, list):
            raise ValueError("session list shape must be a JSON array")
        return raw

    def _expected_reference_fields(
        self,
        request: TaskRequest,
        snapshot: TaskActivitySnapshot,
        fingerprint: dict[str, Any],
    ) -> dict[str, str]:
        metadata = discovery_metadata(
            request=request,
            snapshot=snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        agent_run = agent_run_from_request(request)
        return {
            "profile_identity_digest": canonical_digest(fingerprint),
            "metadata_match_digest": metadata_match_digest(metadata),
            "request_digest": snapshot.request_digest,
            "expected_message_id": expected_message_id(snapshot),
            "prompt_body_digest": prompt_body_digest(agent_run),
            "adapter_version": ADAPTER_VERSION,
        }

    def _reference_drifted(self, reference: OpenCodeActivityReference, expected: dict[str, str]) -> bool:
        return (
            reference.profile_identity_digest != expected["profile_identity_digest"]
            or reference.metadata_match_digest != expected["metadata_match_digest"]
            or reference.request_digest != expected["request_digest"]
            or reference.expected_message_id != expected["expected_message_id"]
            or reference.prompt_body_digest != expected["prompt_body_digest"]
            or reference.adapter_version != expected["adapter_version"]
        )

    def _identity_mismatch(
        self,
        request: TaskRequest,
        context: TaskContext,
        snapshot: TaskActivitySnapshot,
    ) -> str | None:
        computed = canonical_digest(request.model_dump(mode="json"))
        if computed != snapshot.request_digest:
            return "request identity drifted"
        if context.workspace_root.name != snapshot.workspace_identity.attempt_directory_id:
            return "workspace identity drifted"
        try:
            AgentRunRequest.model_validate(thaw_json(request.input))
        except ValidationError:
            return "frozen request is invalid"
        return None

    async def _observe_fingerprint(self, client: OpenCodeHttpClient, secret: bytes) -> dict[str, Any]:
        config = self._require_config()
        identity = await client.get_server_identity()
        advertised = await client.get_profile()
        profile = AcceptedOpenCodeProfile.model_validate(advertised)
        fingerprint = {
            "endpoint_origin": config.origin,
            "tls_identity_digest": config.tls_identity_digest,
            "protocol_profile": profile.protocol_profile,
            "prompt_admission": profile.prompt_admission,
            "prompt_idempotency": profile.prompt_idempotency,
            "server_identity_digest": canonical_digest(identity),
            "project_scope": config.project_scope,
        }
        reject_credentials_in_digest_input(identity)
        reject_credentials_in_digest_input(advertised)
        reject_credentials_in_digest_input(fingerprint)
        text = canonical_json_text(fingerprint)
        secret_text = secret.decode("utf-8")
        if secret_text in text:
            raise ValueError("credentials must not enter dispatch fingerprint")
        return fingerprint

    def _require_config(self) -> OpenCodeAdapterConfig:
        if self._config is None:
            raise ValueError("OpenCode adapter config is required")
        return self._config
