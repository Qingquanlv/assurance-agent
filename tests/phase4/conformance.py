"""Test-only plugin, handler, validator, and effect conformance helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import json

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    CommitValidator,
    DurableEffectHandler,
    EffectIntent,
    InvocationMetadata,
    PluginContribution,
    PluginContractError,
    PluginProvider,
    ResourceClaims,
    TaskContext,
    TaskFailure,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    TaskStatus,
    ValidationContext,
)

_JSON_MEDIA_TYPES = frozenset({"application/json", "application/schema+json"})
_DETERMINISTIC_DIGEST = "0" * 64
_DETERMINISTIC_INVOCATION_ID = "phase4-test"


@dataclass(frozen=True, slots=True)
class PluginExpectation:
    plugin_id: str
    dependencies: tuple[str, ...]
    id_prefix: str


@dataclass(frozen=True, slots=True)
class ExecutedTask:
    outcome: TaskOutcome
    workspace_bytes: Mapping[str, bytes]

    @property
    def status(self) -> TaskStatus:
        return self.outcome.status

    @property
    def output(self) -> JSONValue:
        return self.outcome.output

    @property
    def failure(self) -> TaskFailure | None:
        return self.outcome.failure

    @property
    def stop_reason(self) -> str | None:
        return self.outcome.stop_reason

    @property
    def effects(self) -> tuple[EffectIntent, ...]:
        return self.outcome.effects


def assert_plugin_conforms(provider: PluginProvider, expected: PluginExpectation) -> None:
    descriptor = provider.descriptor()
    contribution = provider.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    try:
        from graph_engine.plugin_api import validate_contribution

        validate_contribution(descriptor, contribution)
    except PluginContractError as error:
        raise AssertionError(f"descriptor/contribution mismatch: {error}") from error
    assert descriptor.plugin_id == expected.plugin_id
    assert tuple(item.plugin_id for item in descriptor.dependencies) == expected.dependencies
    ids = _all_contribution_ids(contribution)
    assert ids
    assert all(item.startswith(expected.id_prefix) for item in ids)
    assert tuple(sorted(ids)) == tuple(ids)
    _assert_canonical_resource_and_schema_bytes(contribution)


async def execute_task(
    handler: TaskHandler,
    request: TaskRequest | JSONValue,
    workspace: Path | None = None,
    *,
    binding_data: JSONValue = None,
) -> ExecutedTask:
    if workspace is None:
        with TemporaryDirectory(prefix="phase4-task-") as temporary:
            return await _execute_in_workspace(
                handler,
                request,
                Path(temporary),
                binding_data=binding_data,
            )
    workspace.mkdir(parents=True, exist_ok=True)
    return await _execute_in_workspace(handler, request, workspace, binding_data=binding_data)


def assert_validator_rejects(
    validator: CommitValidator,
    *,
    reason: str,
    files: tuple[CandidateFile, ...] = (),
    candidate: CandidateWriteSet | None = None,
    context: ValidationContext | None = None,
) -> None:
    write_set = candidate or CandidateWriteSet(
        baseline_tree_id=_DETERMINISTIC_DIGEST,
        candidate_tree_id="1" * 64,
        files=files,
    )
    validation_context = context or ValidationContext(
        invocation_id=_DETERMINISTIC_INVOCATION_ID,
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )
    result = validator.validate(write_set, validation_context)
    assert result.accepted is False
    assert result.reason == reason


async def assert_effect_idempotent(
    handler: DurableEffectHandler,
    intent: EffectIntent,
    idempotency_key: str,
) -> None:
    first = await handler.apply(intent, idempotency_key)
    second = await handler.apply(intent, idempotency_key)
    assert first.status == "applied"
    assert second.status == "applied"
    assert _receipt_bytes(first.receipt) == _receipt_bytes(second.receipt)
    reconciled = await handler.reconcile(intent, idempotency_key)
    assert reconciled.status == "applied"
    assert _receipt_bytes(reconciled.receipt) == _receipt_bytes(first.receipt)


def _all_contribution_ids(contribution: PluginContribution) -> tuple[str, ...]:
    return (
        tuple(contribution.task_handlers)
        + tuple(contribution.commit_validators)
        + tuple(entry.schema_id for entry in contribution.schemas)
        + tuple(entry.resource_id for entry in contribution.resources)
        + tuple(entry.kind for entry in contribution.effects)
        + tuple(entry.capability_id for entry in contribution.bindings)
    )


def _assert_canonical_resource_and_schema_bytes(contribution: PluginContribution) -> None:
    for schema in contribution.schemas:
        _assert_canonical_bytes(schema.schema_id, schema.media_type, schema.content)
    for resource in contribution.resources:
        _assert_canonical_bytes(resource.resource_id, resource.media_type, resource.content)


def _assert_canonical_bytes(item_id: str, media_type: str, content: bytes) -> None:
    payload = bytes(content)
    if media_type not in _JSON_MEDIA_TYPES:
        return
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise AssertionError(f"{item_id} content is not valid JSON") from error
    expected = canonical_json_bytes(cast(JSONValue, parsed))
    if payload != expected:
        raise AssertionError(f"{item_id} content is not canonical JSON")


async def _execute_in_workspace(
    handler: TaskHandler,
    request: TaskRequest | JSONValue,
    workspace: Path,
    *,
    binding_data: JSONValue,
) -> ExecutedTask:
    task_request = request if isinstance(request, TaskRequest) else _task_request(request, binding_data)
    outcome = await handler.execute(
        task_request,
        TaskContext(
            workspace_root=workspace,
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=task_request.invocation,
        ),
    )
    return ExecutedTask(outcome=outcome, workspace_bytes=_workspace_bytes(workspace))


def _task_request(payload: JSONValue, binding_data: JSONValue) -> TaskRequest:
    invocation = InvocationMetadata(
        invocation_id=_DETERMINISTIC_INVOCATION_ID,
        lock_digest=_DETERMINISTIC_DIGEST,
        composition_digest=_DETERMINISTIC_DIGEST,
        entrypoint="phase4",
    )
    return TaskRequest(
        invocation_id=invocation.invocation_id,
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        capability_id="test.phase4.capability",
        binding_data=binding_data,
        invocation=invocation,
        attempt=1,
        input=payload,
    )


def _workspace_bytes(root: Path) -> Mapping[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return MappingProxyType(files)


def _receipt_bytes(receipt: JSONValue) -> bytes:
    return canonical_json_bytes(cast(JSONValue, thaw_json(receipt)))


__all__ = [
    "ExecutedTask",
    "PluginExpectation",
    "assert_effect_idempotent",
    "assert_plugin_conforms",
    "assert_validator_rejects",
    "execute_task",
]
