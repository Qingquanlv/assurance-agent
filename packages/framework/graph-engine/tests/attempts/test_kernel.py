from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from graph_engine.application.runtime_context import AttemptKernelPort
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import CommittedTaskResult, PermanentTaskFailure, RejectedTaskResult
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import (
    MemoryResourceAuthorizationStore,
    ResourceAuthorizationError,
)
from graph_engine.plugin_api import (
    CommitValidator,
    ResourceClaims,
    TaskWorkspaceBinding,
    ValidationResult,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None

    async def open_or_create(self, attempt_key: AttemptKey, claims: ResourceClaims) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims)
        return self.binding

    async def seal(self, binding: TaskWorkspaceBinding):
        return await self.inner.seal(binding)

    async def prepare(self, binding: TaskWorkspaceBinding, sealed):
        return await self.inner.prepare(binding, sealed)

    async def promote(self, prepared):
        return await self.inner.promote(prepared)

    async def recover_promotion(self, prepared):
        return await self.inner.recover_promotion(prepared)


class _WritingExecutor:
    def __init__(self, workspace: _RecordingWorkspace, content: bytes = b"committed") -> None:
        self.workspace = workspace
        self.content = content
        self.calls = 0

    async def execute(self, validated_input: RunInput, context: AttemptExecutionContext) -> RunOutput:
        del validated_input, context
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        (binding.write_root / "out.txt").write_bytes(self.content)
        return RunOutput(status="ok")


class _OtherOutput(BaseModel):
    unexpected: str


class _InvalidOutputExecutor:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0

    async def execute(self, validated_input: RunInput, context: AttemptExecutionContext) -> _OtherOutput:
        del validated_input, context
        self.calls += 1
        assert self.workspace is not None
        binding = self.workspace.binding
        assert binding is not None
        (binding.write_root / "out.txt").write_bytes(b"invalid")
        return _OtherOutput(unexpected="nope")


class _CrashWithoutReconcile:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0

    async def execute(self, validated_input: RunInput, context: AttemptExecutionContext) -> RunOutput:
        del validated_input, context
        self.calls += 1
        assert self.workspace is not None
        binding = self.workspace.binding
        assert binding is not None
        (binding.write_root / "out.txt").write_bytes(b"partial")
        raise RuntimeError("in-flight activity")


class _RejectingValidator:
    def validate(self, staged: object, context: object) -> ValidationResult:
        del staged, context
        return ValidationResult(accepted=False, reason="policy rejected")


class _ExplodingValidator:
    def validate(self, staged: object, context: object) -> ValidationResult:
        del staged, context
        raise RuntimeError("validator crashed")


def graph_revision() -> str:
    return canonical_digest({"revision": "kernel-test"})


def contract(*, validators: tuple[str, ...] = ()) -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(writes=("out.txt",)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=validators,
    )


def make_kernel(
    tmp_path: Path,
    *,
    executor: Any = None,
    transaction_cut=None,
    validators: Mapping[str, CommitValidator] | None = None,
    validator_ids: tuple[str, ...] = (),
):
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = executor if executor is not None else _WritingExecutor(workspace)
    writer.workspace = workspace
    resolved = resolve_contract(contract(validators=validator_ids), executor=writer)
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=graph_revision(),
        validators=validators or {},
        transaction_cut=transaction_cut,
    )
    validated = RunInput(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=graph_revision(),
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=4,
    )
    return kernel, key, resolved, validated, context, writer, project, store


async def test_happy_path_trace_commits_receipt(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, executor, project, store = make_kernel(tmp_path)
    try:
        trace: list[str] = []
        result = await kernel.execute_or_recover(key, resolved, validated, context, trace=trace)
        assert trace == [
            "adopt_or_create",
            "authorize_resources",
            "begin_workspace",
            "execute",
            "validate_output",
            "seal_candidate",
            "run_validators",
            "durable_prepare",
            "promote",
            "settle_effects",
            "publish_receipt",
        ]
        assert isinstance(result, CommittedTaskResult)
        assert result.output == RunOutput(status="ok")
        assert len(result.receipt.receipt_id) >= 1
        assert len(result.receipt.receipt_digest) == 64
        assert executor.calls == 1
        assert (project / "out.txt").read_bytes() == b"committed"
        port: AttemptKernelPort = kernel
        assert port is kernel
    finally:
        store.close()


async def test_kernel_port_exposes_execute_or_recover() -> None:
    assert "execute_or_recover" in AttemptKernelPort.__dict__ or hasattr(
        AttemptKernelPort, "execute_or_recover"
    )
    assert hasattr(AssuranceAttemptKernel, "execute_or_recover")
    annotations = inspect_execute_or_recover()
    assert "attempt_key" in annotations
    assert "contract" in annotations or "resolved" in annotations


def inspect_execute_or_recover() -> set[str]:
    import inspect

    signature = inspect.signature(AssuranceAttemptKernel.execute_or_recover)
    return set(signature.parameters)


async def _assert_released(kernel: AssuranceAttemptKernel, key: AttemptKey, fencing_token: int) -> None:
    snapshot = await kernel.journal.load(key)
    assert snapshot is not None
    assert snapshot.terminal is not None
    assert snapshot.released is True
    with pytest.raises(ResourceAuthorizationError, match="no active authorization"):
        await kernel.arbiter.assert_usable(key, fencing_token=fencing_token)


async def test_rejected_terminal_replay_returns_same_rejection(tmp_path: Path) -> None:
    validator_id = "assurance.execution.validator.policy.v1"
    kernel, key, resolved, validated, context, executor, _project, store = make_kernel(
        tmp_path,
        validators={validator_id: _RejectingValidator()},
        validator_ids=(validator_id,),
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, RejectedTaskResult)
        assert first.reason == "policy rejected"
        assert first.writes_promoted is False
        await _assert_released(kernel, key, context.fencing_token)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert isinstance(replay, RejectedTaskResult)
        assert executor.calls == 1
        await _assert_released(kernel, key, context.fencing_token)
    finally:
        store.close()


async def test_permanent_validator_terminal_replay_returns_same_failure(tmp_path: Path) -> None:
    validator_id = "assurance.execution.validator.boom.v1"
    kernel, key, resolved, validated, context, executor, _project, store = make_kernel(
        tmp_path,
        validators={validator_id: _ExplodingValidator()},
        validator_ids=(validator_id,),
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, PermanentTaskFailure)
        assert first.kind == "internal"
        assert "validator crashed" in first.message
        await _assert_released(kernel, key, context.fencing_token)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert isinstance(replay, PermanentTaskFailure)
        assert executor.calls == 1
    finally:
        store.close()


async def test_invalid_output_terminates_releases_and_replays(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, executor, _project, store = make_kernel(
        tmp_path,
        executor=_InvalidOutputExecutor(),
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, PermanentTaskFailure)
        assert first.kind == "invalid_output"
        await _assert_released(kernel, key, context.fencing_token)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert executor.calls == 1
    finally:
        store.close()


async def test_unadoptable_in_flight_activity_terminates_releases_and_replays(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, executor, _project, store = make_kernel(
        tmp_path,
        executor=_CrashWithoutReconcile(),
    )
    try:
        with pytest.raises(RuntimeError, match="in-flight activity"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, PermanentTaskFailure)
        assert first.kind == "internal"
        assert "cannot be adopted" in first.message
        await _assert_released(kernel, key, context.fencing_token)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert executor.calls == 1
    finally:
        store.close()
