from __future__ import annotations

import json
from typing import Any

import httpx

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import canonical_digest, reject_credentials_in_digest_input, thaw_json
from graph_engine.plugin_api import TaskActivityReconcileResult, TaskContext, TaskRequest

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.observe.sse import parse_sse_frames, reduce_sse_frames
from agent_runtime_opencode.observe.state import classify_provider_state, parse_closed_terminal_result
from agent_runtime_opencode.result import (
    reduce_terminal,
    result_candidate_satisfies_contract,
    select_unique_contract_valid_result,
)
from agent_runtime_opencode.session.discovery import OpenCodeActivityReference
from agent_runtime_opencode.transport.connection import PROVIDER_ERRORS
from agent_runtime_opencode.transport.http import OpenCodeHttpClient, canonical_json_text
from agent_runtime_opencode.transport.profile import resolve_advertised_profile


async def _observe_bound(
    client: OpenCodeHttpClient,
    request: TaskRequest,
    context: TaskContext,
    reference: OpenCodeActivityReference,
    record: dict[str, Any],
    *,
    agent_run: AgentRunRequest,
    canaries: tuple[str, ...],
) -> TaskActivityReconcileResult:
    session_id = reference.session_id
    if not session_id:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="bound session identity is unknown",
        )
    cursor: str | None = None
    try:
        payload = await client.open_sse()
        reduced = reduce_sse_frames(
            parse_sse_frames(payload),
            session_id=session_id,
            heartbeat=context.heartbeat,
        )
        if reduced.malformed:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="malformed identity-bearing SSE event",
            )
        cursor = reduced.cursor
        if cursor:
            follow = await client.open_sse(cursor=cursor)
            follow_reduced = reduce_sse_frames(
                parse_sse_frames(follow),
                session_id=session_id,
                heartbeat=context.heartbeat,
            )
            if follow_reduced.malformed:
                return TaskActivityReconcileResult(
                    status="indeterminate",
                    reason="malformed identity-bearing SSE event",
                )
    except httpx.TimeoutException:
        pass
    except (httpx.TransportError, json.JSONDecodeError, ValueError) as error:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=str(error) or "provider observation is indeterminate",
        )
    context.heartbeat()
    try:
        status_map = await client.get_status()
        session = await client.get_session(session_id)
        if not isinstance(session, dict):
            session = record
        messages = await client.list_messages(session_id)
    except PROVIDER_ERRORS as error:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=str(error) or "provider observation is indeterminate",
        )
    kind = classify_provider_state(
        session_id=session_id,
        status_map=status_map,
        session=session,
        messages=messages,
    )
    dumped = thaw_json(reference.model_dump(mode="json"))
    status_record = status_map.get(session_id) if isinstance(status_map, dict) else None
    busy = isinstance(status_record, dict) and status_record.get("type") == "busy"
    selected_result = None
    if busy:
        selected_result = select_unique_contract_valid_result(
            messages,
            agent_run=agent_run,
            request=request,
            canaries=canaries,
        )
        if selected_result is not None:
            kind = "succeeded"
    if kind == "running":
        return TaskActivityReconcileResult(status="running", reference=dumped)
    if kind == "succeeded":
        if busy:
            try:
                candidate = selected_result or parse_closed_terminal_result(messages)
            except ValueError:
                return TaskActivityReconcileResult(status="running", reference=dumped)
            if not result_candidate_satisfies_contract(
                candidate,
                agent_run=agent_run,
                request=request,
                canaries=canaries,
            ):
                return TaskActivityReconcileResult(status="running", reference=dumped)
            try:
                await client.abort(session_id)
            except PROVIDER_ERRORS as error:
                return TaskActivityReconcileResult(
                    status="indeterminate",
                    reason=str(error) or "busy session abort is indeterminate",
                )
    try:
        diff = await client.get_session_diff(session_id)
    except PROVIDER_ERRORS as error:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=str(error) or "provider observation is indeterminate",
        )
    return TaskActivityReconcileResult(
        status="terminal",
        reference=dumped,
        outcome=reduce_terminal(
            kind=kind,
            session=session,
            messages=messages,
            agent_run=agent_run,
            request=request,
            diff=diff,
            canaries=canaries,
            selected_result=selected_result,
        ),
    )


async def _observe_fingerprint(
    client: OpenCodeHttpClient,
    config: OpenCodeAdapterConfig,
    secret: bytes,
) -> dict[str, Any]:
    identity = await client.get_server_identity()
    advertised = await client.get_profile()
    profile = resolve_advertised_profile(advertised, config)
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
    reject_credentials_in_digest_input(fingerprint)
    text = canonical_json_text(fingerprint)
    secret_text = secret.decode("utf-8")
    if secret_text and secret_text in text:
        raise ValueError("credentials must not enter dispatch fingerprint")
    return fingerprint
