from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, rebind_agent_run_workspace
from agent_runtime_contracts.wire.schema import canonical_digest, thaw_json
from graph_engine.plugin_api import TaskActivitySnapshot, TaskContext, TaskRequest

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.session.discovery import (
    ADAPTER_VERSION,
    OpenCodeActivityReference,
    adapter_source_digest,
    agent_run_from_request,
    discovery_metadata,
    expected_message_id,
    metadata_match_digest,
    prompt_body_digest,
)


def workspace_identity_digest_for(context: TaskContext) -> str:
    return canonical_digest(
        {
            "project_root": str(context.project_root.resolve()),
            "write_root": str(context.write_root.resolve()),
        }
    )


def _dispatch_fingerprint(fingerprint: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    return {
        **fingerprint,
        "workspace_identity_digest": workspace_identity_digest_for(context),
    }


def _expected_reference_fields(
    request: TaskRequest,
    snapshot: TaskActivitySnapshot,
    fingerprint: dict[str, Any],
    agent_run: AgentRunRequest,
) -> dict[str, str]:
    metadata = discovery_metadata(
        request=request,
        snapshot=snapshot,
        adapter_source_digest=adapter_source_digest(),
    )
    fields = {
        "profile_identity_digest": canonical_digest(fingerprint),
        "metadata_match_digest": metadata_match_digest(metadata),
        "request_digest": snapshot.request_digest,
        "expected_message_id": expected_message_id(snapshot),
        "prompt_body_digest": prompt_body_digest(agent_run),
        "adapter_version": ADAPTER_VERSION,
    }
    config = OpenCodeAdapterConfig.from_request(request)
    if config.parent_session_id:
        fields["parent_session_id"] = config.parent_session_id
        fields["worktree"] = str(config.project_scope)
    return fields


def _reference_drifted(reference: OpenCodeActivityReference, expected: dict[str, str]) -> bool:
    return (
        reference.profile_identity_digest != expected["profile_identity_digest"]
        or reference.metadata_match_digest != expected["metadata_match_digest"]
        or reference.request_digest != expected["request_digest"]
        or reference.expected_message_id != expected["expected_message_id"]
        or reference.prompt_body_digest != expected["prompt_body_digest"]
        or reference.adapter_version != expected["adapter_version"]
        or reference.parent_session_id != expected.get("parent_session_id")
        or (expected.get("worktree") is not None and reference.worktree != expected.get("worktree"))
    )


def _identity_mismatch(
    request: TaskRequest,
    context: TaskContext,
    snapshot: TaskActivitySnapshot,
) -> str | None:
    computed = canonical_digest(request.model_dump(mode="json"))
    if computed != snapshot.request_digest:
        return "request identity drifted"
    if context.workspace_identity.identity_digest != snapshot.workspace_identity.identity_digest:
        return "workspace identity drifted"
    live = workspace_identity_digest_for(context)
    fingerprint = thaw_json(snapshot.dispatch_fingerprint) if snapshot.dispatch_fingerprint else None
    if isinstance(fingerprint, dict):
        stored = fingerprint.get("workspace_identity_digest")
        if stored not in {None, live}:
            return "workspace identity drifted"
    try:
        AgentRunRequest.model_validate(thaw_json(request.input))
    except ValidationError:
        return "frozen request is invalid"
    return None


def _effective_agent_run(request: TaskRequest, context: TaskContext) -> AgentRunRequest:
    return rebind_agent_run_workspace(
        agent_run_from_request(request),
        project_root=context.project_root,
        write_root=context.write_root,
    )
