"""Build AgentRunRequest payloads for capability prepare handlers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from graph_engine.plugin_api import FrozenModel, TaskOutcome

from agent_runtime_contracts.wire.models import (
    AgentRunRequest,
    AgentWorkspaceV1,
    InstructionPart,
    JSONValue,
    ResultContract,
    prompt_model_json,
    with_validation_retry,
)
from agent_runtime_contracts.ops.binding import AgentBindingDataV1
from agent_runtime_contracts.wire.schema import canonical_digest

BOUNDED_PROFILES: Mapping[str, str] = MappingProxyType(
    {
        "aa-archiver": "assurance-v1-archiver",
        "aa-doc-author": "assurance-v1-doc-author",
        "aa-executor": "assurance-v1-executor",
        "aa-explorer": "assurance-v1-explorer",
        "aa-reporter": "assurance-v1-reporter",
        "aa-reviewer": "assurance-v1-reviewer",
        "aa-test-author": "assurance-v1-test-author",
    }
)
_OUTSIDE_PROJECT_WRITE_ROOT = "qa/.staging/write"
_PROJECT_ROOT_WRITE_ROOT = ".staging/write"


class WorkspaceRoots(Protocol):
    @property
    def project_root(self) -> Path: ...

    @property
    def write_root(self) -> Path: ...


def logical_write_root(roots: WorkspaceRoots) -> str:
    try:
        relative = roots.write_root.resolve().relative_to(roots.project_root.resolve()).as_posix()
    except ValueError:
        return _OUTSIDE_PROJECT_WRITE_ROOT
    if relative in {".", ""}:
        return _PROJECT_ROOT_WRITE_ROOT
    return relative


def agent_workspace(
    roots: WorkspaceRoots,
    *,
    allowed_outputs: Sequence[str],
    agent_profile: str,
    scope_id: str,
) -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": logical_write_root(roots),
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
        "read_roots": (),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def result_contract_from(schema_id: str, schema_document: JSONValue) -> ResultContract:
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(schema_document),
        delivery_mode="assistant_json_local_v1",
        schema_document=schema_document,
    )


def agent_run_request(
    *,
    instructions: tuple[InstructionPart, ...],
    validation_error: str | None,
    result: ResultContract,
    binding: AgentBindingDataV1,
    roots: WorkspaceRoots,
    allowed_outputs: Sequence[str],
    scope_id: str,
) -> AgentRunRequest:
    return AgentRunRequest(
        instructions=with_validation_retry(instructions, validation_error),
        result_contract=result,
        execution=binding.execution,
        workspace=agent_workspace(
            roots,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
            scope_id=scope_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )


def skill_request(
    *,
    skill_text: str,
    business: FrozenModel,
    binding: AgentBindingDataV1,
    result: ResultContract,
    roots: WorkspaceRoots,
    allowed_outputs: Sequence[str],
    scope_id: str,
    business_extra: Mapping[str, Any] | None = None,
) -> AgentRunRequest:
    business_input = prompt_model_json(business)
    if business_extra is not None:
        business_input.update(business_extra)
    return agent_run_request(
        instructions=(
            InstructionPart.text("text/plain", skill_text),
            InstructionPart.from_json(business_input),
        ),
        validation_error=getattr(business, "validation_error", None),
        result=result,
        binding=binding,
        roots=roots,
        allowed_outputs=allowed_outputs,
        scope_id=scope_id,
    )


def prepared_outcome(request: AgentRunRequest) -> TaskOutcome:
    return TaskOutcome.succeeded(request.model_dump(mode="json"))
