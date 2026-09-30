from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

import pytest
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, FrozenExecutionSelection
from agent_runtime_contracts.wire.schema import canonical_json_bytes, thaw_json
from graph_engine import SecretHandleUnauthorized
from graph_engine.plugin_api import TaskContext, TaskWorkspaceIdentity


RecoveryProfile = Literal["durable_reference", "confined_process"]
AdapterCut = Literal[
    "prepared",
    "before_dispatch",
    "after_dispatch_before_bind",
    "after_bind",
    "after_terminal_receipt",
    "cancel_before_provider",
    "ambiguous_create",
    "dead_host",
    "success",
]
ReconcileStatus = Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]
_FORBIDDEN_CONTEXT = {"ledger", "store", "lock", "checkpoint", "registry", "event_writer"}


class PreparedAdapterFixture:
    __slots__ = (
        "provider_calls",
        "initial_event_kinds",
        "request_bytes",
        "expected_request_bytes",
        "request_payload",
        "workspace_identity",
        "activity_state",
        "context_exposed",
        "secret_handles_resolved",
    )

    def __init__(
        self,
        *,
        provider_calls: int,
        initial_event_kinds: tuple[str, ...],
        request_bytes: bytes,
        expected_request_bytes: bytes,
        request_payload: object,
        workspace_identity: object,
        activity_state: str,
        context_exposed: tuple[str, ...],
        secret_handles_resolved: tuple[str, ...],
    ) -> None:
        self.provider_calls = provider_calls
        self.initial_event_kinds = initial_event_kinds
        self.request_bytes = request_bytes
        self.expected_request_bytes = expected_request_bytes
        self.request_payload = request_payload
        self.workspace_identity = workspace_identity
        self.activity_state = activity_state
        self.context_exposed = context_exposed
        self.secret_handles_resolved = secret_handles_resolved


class CutResult:
    __slots__ = (
        "cut",
        "event_kinds",
        "activity_state",
        "reconcile_status",
        "cancel_status",
        "outcome_status",
        "attempt",
        "workspace_identity",
        "provider_calls",
        "receipt_count",
        "instruction_bytes",
        "host_calls",
        "scheduler_drives",
        "context_exposed",
    )

    def __init__(
        self,
        *,
        cut: AdapterCut,
        event_kinds: tuple[str, ...],
        activity_state: str | None,
        reconcile_status: ReconcileStatus | None,
        cancel_status: str | None,
        outcome_status: str | None,
        attempt: int | None,
        workspace_identity: object,
        provider_calls: int,
        receipt_count: int,
        instruction_bytes: bytes,
        host_calls: tuple[str, ...] = (),
        scheduler_drives: int = 0,
        context_exposed: tuple[str, ...] = (),
    ) -> None:
        self.cut = cut
        self.event_kinds = event_kinds
        self.activity_state = activity_state
        self.reconcile_status = reconcile_status
        self.cancel_status = cancel_status
        self.outcome_status = outcome_status
        self.attempt = attempt
        self.workspace_identity = workspace_identity
        self.provider_calls = provider_calls
        self.receipt_count = receipt_count
        self.instruction_bytes = instruction_bytes
        self.host_calls = host_calls
        self.scheduler_drives = scheduler_drives
        self.context_exposed = context_exposed


class RecoveryOutcome:
    __slots__ = ("status", "attempt", "reason")

    def __init__(self, *, status: ReconcileStatus, attempt: int | None, reason: str | None) -> None:
        self.status = status
        self.attempt = attempt
        self.reason = reason


@runtime_checkable
class RuntimeAdapterHarness(Protocol):
    recovery_profile: RecoveryProfile

    async def prepared_fixture(self) -> PreparedAdapterFixture: ...

    async def run_to_cut(self, cut: AdapterCut) -> CutResult: ...

    def durable_bytes(self) -> tuple[bytes, ...]: ...

    def provider_call_count(self, operation: str) -> int: ...

    async def recover_live_activity(self) -> RecoveryOutcome: ...

    async def recover_from_dead_host(self) -> RecoveryOutcome: ...

    def unauthorized_secret_resolve(self, handle: str) -> None: ...

    def delete_checkpoint(self) -> None: ...


@pytest.mark.asyncio
async def assert_common_runtime_adapter_contract(harness: RuntimeAdapterHarness) -> None:
    fixture = await harness.prepared_fixture()
    assert fixture.provider_calls == 0
    assert fixture.request_bytes
    assert fixture.request_bytes == fixture.expected_request_bytes
    assert fixture.activity_state == "prepared"
    _assert_prepared_workspace_identity(fixture.workspace_identity)

    _assert_strict_request_parsing(fixture)
    _assert_no_instruction_or_routing_mutation(fixture)
    _assert_authorized_secrets_only(harness)

    before_dispatch = await harness.run_to_cut("before_dispatch")
    assert before_dispatch.activity_state == "prepared"
    assert before_dispatch.provider_calls == 0
    assert harness.provider_call_count("dispatch") == 0
    assert "execute" not in before_dispatch.host_calls

    bind = await harness.run_to_cut("after_bind")
    assert bind.activity_state in {"bound", "dispatch_started"}
    assert bind.attempt == 1
    assert "execute" in bind.host_calls
    assert bind.instruction_bytes == fixture.request_bytes
    assert bind.instruction_bytes == fixture.expected_request_bytes
    _assert_no_ledger_store_exposure(bind.context_exposed)

    cancel = await harness.run_to_cut("cancel_before_provider")
    assert cancel.cancel_status in {"acknowledged", "terminal", "indeterminate"}
    assert cancel.provider_calls == 0
    assert harness.provider_call_count("dispatch") == 0
    assert "cancel" in cancel.host_calls
    assert "execute" not in cancel.host_calls

    success = await harness.run_to_cut("success")
    assert success.outcome_status == "succeeded"
    assert success.attempt == 1
    assert "execute" in success.host_calls
    _assert_prepared_workspace_identity(success.workspace_identity)

    replay = await harness.run_to_cut("success")
    assert replay.outcome_status == "succeeded"
    assert replay.provider_calls == success.provider_calls
    assert replay.scheduler_drives > success.scheduler_drives
    assert replay.attempt == success.attempt == 1
    _assert_same_attempt_workspace(success.workspace_identity, replay.workspace_identity)

    durable = b"\n".join(harness.durable_bytes())
    assert b"canary-secret-value" not in durable

    calls_before_checkpoint = harness.provider_call_count("dispatch")
    harness.delete_checkpoint()
    after_checkpoint = await harness.run_to_cut("success")
    assert after_checkpoint.outcome_status == "succeeded"
    assert harness.provider_call_count("dispatch") == calls_before_checkpoint
    assert after_checkpoint.scheduler_drives > replay.scheduler_drives
    _assert_same_attempt_workspace(success.workspace_identity, after_checkpoint.workspace_identity)

    receipt = await harness.run_to_cut("after_terminal_receipt")
    assert receipt.receipt_count == 1
    assert receipt.reconcile_status == "terminal"
    assert "reconcile" in receipt.host_calls or "execute" in receipt.host_calls

    await _assert_honest_recovery_profile(harness)


def _assert_prepared_workspace_identity(value: object) -> None:
    identity = _workspace_identity(value)
    assert identity.layout_schema_version == "1"
    assert identity.attempt_id
    assert "/" not in identity.attempt_id
    assert identity.identity_digest


def _assert_same_attempt_workspace(left: object, right: object) -> None:
    first = _workspace_identity(left)
    second = _workspace_identity(right)
    assert first.layout_schema_version == second.layout_schema_version == "1"
    assert first.attempt_id == second.attempt_id
    assert first.identity_digest == second.identity_digest
    assert first.attempt_id
    assert "/" not in first.attempt_id


def _workspace_identity(value: object) -> TaskWorkspaceIdentity:
    if isinstance(value, TaskWorkspaceIdentity):
        return value
    if isinstance(value, dict):
        return TaskWorkspaceIdentity.model_validate(value)
    raise AssertionError(f"workspace identity is not public TaskWorkspaceIdentity: {type(value)!r}")


def _assert_strict_request_parsing(fixture: PreparedAdapterFixture) -> None:
    payload = thaw_json(fixture.request_payload)
    assert isinstance(payload, dict)
    assert canonical_json_bytes(payload) == fixture.request_bytes
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate({**payload, "model_fallback": "auto"})
    with pytest.raises(ValidationError):
        FrozenExecutionSelection.model_validate(
            {
                **payload["execution"],  # type: ignore[index]
                "provider_model": "a,fallback,b",
            }
        )


def _assert_no_instruction_or_routing_mutation(fixture: PreparedAdapterFixture) -> None:
    request = AgentRunRequest.model_validate(thaw_json(fixture.request_payload))
    assert request.canonical_bytes() == fixture.request_bytes
    assert request.canonical_bytes() == fixture.expected_request_bytes
    assert request.execution.provider_model == "provider_default"
    lowered = request.execution.provider_model.lower()
    assert "fallback" not in lowered
    assert "route:" not in lowered
    assert len(request.request_policy_digest) == 64
    assert len(request.request_config_digest) == 64


def _assert_no_ledger_store_exposure(context_exposed: tuple[str, ...]) -> None:
    assert context_exposed
    assert _FORBIDDEN_CONTEXT.isdisjoint(context_exposed)
    assert "write_root" in context_exposed
    assert set(TaskContext.__dataclass_fields__) >= {
        "project_root",
        "write_root",
        "workspace_identity",
        "heartbeat",
        "activity",
        "secrets",
    }


def _assert_authorized_secrets_only(harness: RuntimeAdapterHarness) -> None:
    with pytest.raises(SecretHandleUnauthorized):
        harness.unauthorized_secret_resolve("unlocked.foreign-secret")


async def _assert_honest_recovery_profile(harness: RuntimeAdapterHarness) -> None:
    if harness.recovery_profile == "durable_reference":
        recovered = await harness.recover_live_activity()
        assert recovered.status in {"running", "terminal"}
        assert recovered.status not in {"absent", "not_dispatched"}
        assert recovered.attempt == 1
        assert harness.provider_call_count("create") <= 1
        return
    recovered = await harness.recover_from_dead_host()
    assert recovered.status == "indeterminate"
    assert recovered.status not in {"running", "terminal", "absent", "not_dispatched"}
    assert recovered.attempt == 1
