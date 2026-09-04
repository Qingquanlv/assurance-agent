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
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    TaskAttemptContract,
    TerminalReceiptRef,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel, AttemptIntegrityError
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS
from graph_engine.effects.state import MemoryEffectState
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_journal import (
    ATTEMPT_JOURNAL_SCHEMA_VERSION,
    MemoryAttemptJournal,
)
from graph_engine.persistence.resource_authorization import (
    MemoryResourceAuthorizationStore,
    ResourceAuthorizationError,
)
from graph_engine.plugin_api import (
    CommitValidator,
    DirectoryIdentity,
    EffectApplyResult,
    EffectIntent,
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedFile,
    SealedWriteSet,
    TaskWorkspaceBinding,
    TaskWorkspaceIdentity,
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

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[RunOutput]:
        del validated_input
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_bytes(self.content)
        return ExecutedAttemptResult(output=RunOutput(status="ok"))


class _OtherOutput(BaseModel):
    unexpected: str


class _NonCanonicalOutput(BaseModel):
    score: float


class _InvalidOutputExecutor:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_OtherOutput]:
        del validated_input
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_bytes(b"invalid")
        return ExecutedAttemptResult(output=_OtherOutput(unexpected="nope"))


class _NonCanonicalOutputExecutor:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_NonCanonicalOutput]:
        del validated_input
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_bytes(b"non-canonical")
        return ExecutedAttemptResult(output=_NonCanonicalOutput(score=float("nan")))


class _CrashWithoutReconcile:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0

    async def execute(self, validated_input: RunInput, scope: AuthorizedAttemptScope) -> RunOutput:
        del validated_input
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_bytes(b"partial")
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


def contract(
    *,
    validators: tuple[str, ...] = (),
    output_model: type[BaseModel] = RunOutput,
) -> TaskAttemptContract[RunInput, Any]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=output_model,
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
    output_model: type[BaseModel] = RunOutput,
):
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = executor if executor is not None else _WritingExecutor(workspace)
    writer.workspace = workspace
    resolved = resolve_contract(
        contract(validators=validator_ids, output_model=output_model),
        executor=writer,
    )
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
            "record_terminal",
            "release_resources",
            "record_release_proof",
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
        first_snapshot = await kernel.journal.load(key)
        assert first_snapshot is not None
        assert first_snapshot.terminal is not None
        assert first_snapshot.terminal.output == {"status": "ok"}
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert isinstance(replay, PermanentTaskFailure)
        replay_snapshot = await kernel.journal.load(key)
        assert replay_snapshot is not None
        assert replay_snapshot.terminal is not None
        assert replay_snapshot.terminal.output == {"status": "ok"}
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
        first_snapshot = await kernel.journal.load(key)
        assert first_snapshot is not None
        assert first_snapshot.terminal is not None
        assert first_snapshot.terminal.output is None
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        replay_snapshot = await kernel.journal.load(key)
        assert replay_snapshot is not None
        assert replay_snapshot.terminal is not None
        assert replay_snapshot.terminal.output is None
        assert executor.calls == 1
    finally:
        store.close()


async def test_noncanonical_valid_output_terminates_releases_and_replays(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, executor, _project, store = make_kernel(
        tmp_path,
        executor=_NonCanonicalOutputExecutor(),
        output_model=_NonCanonicalOutput,
    )
    try:
        trace: list[str] = []
        first = await kernel.execute_or_recover(
            key,
            resolved,
            validated,
            context,
            trace=trace,
        )
        assert first == PermanentTaskFailure(
            kind="configuration",
            message="Out of range float values are not JSON compliant",
        )
        assert trace == ["adopt_or_create", "authorize_resources", "begin_workspace", "execute"]
        await _assert_released(kernel, key, context.fencing_token)
        first_snapshot = await kernel.journal.load(key)
        assert first_snapshot is not None
        assert first_snapshot.terminal is not None
        assert first_snapshot.terminal.output is None

        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        replay_snapshot = await kernel.journal.load(key)
        assert replay_snapshot is not None
        assert replay_snapshot.terminal is not None
        assert replay_snapshot.terminal.output is None
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


DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"


class ValidOutput(BaseModel):
    value: str


class TransactionCrash(RuntimeError):
    pass


def valid_delivery_payload() -> dict[str, int]:
    return {"n": 1}


_CANONICAL_DIRECTORY_IDENTITY = DirectoryIdentity(
    path_digest="6" * 64,
    device=0,
    inode=0,
    identity_digest="93e3d945e68bc3338d508b738c63cf143f5bfdc387eea44402610b82f924b72d",
)
_CANONICAL_WORKSPACE_IDENTITY = TaskWorkspaceIdentity(
    task_id="canonical-attempt",
    attempt=1,
    attempt_id="canonical-attempt-1",
    output_paths=("out.txt",),
    project_digest="1" * 64,
    write_root_digest="2" * 64,
    identity_digest="a204dbac8c4dfc52a29b6f807859fd2cebb4e36c058005098aaa90b54666e801",
)
_CANONICAL_SEALED = SealedWriteSet(
    files=(
        SealedFile(
            path="out.txt",
            before_sha256=None,
            before_mode=None,
            after_sha256="7e65ad97e8760c641674fc508dfae789c0bd04930cc5d85125368ab1f101e63e",
            after_mode=0o644,
            content=b"canonical output\n",
        ),
    ),
    sealed_digest="b5c8b4cc1bdf3d8f54cbe7aa967326cfda9f043c7489d15e8d18aefc0b188aa8",
)
_CANONICAL_PREPARED = PreparedWorkspaceRef(
    identity=_CANONICAL_WORKSPACE_IDENTITY,
    sealed=_CANONICAL_SEALED,
    prepared_digest="da4757b715e099fcb5d0656a9a368218b930124ca987bf3e7b6f84c73fd7decf",
)
_CANONICAL_PROMOTION = PromotionReceipt(
    identity_digest="5" * 64,
    staged_digest="b5c8b4cc1bdf3d8f54cbe7aa967326cfda9f043c7489d15e8d18aefc0b188aa8",
    receipt_digest="9d2991483107ab952ce587de62522d33875b8f609ea69a3e7cadd3e5909bcc62",
)


class _CanonicalJournalWorkspace:
    def __init__(self) -> None:
        self.binding = TaskWorkspaceBinding(
            identity=_CANONICAL_WORKSPACE_IDENTITY,
            project_root=Path("/canonical/project"),
            write_root=Path("/canonical/write"),
            project_root_identity=_CANONICAL_DIRECTORY_IDENTITY,
            write_root_identity=_CANONICAL_DIRECTORY_IDENTITY,
        )

    async def open_or_create(self, attempt_key: AttemptKey, claims: ResourceClaims) -> TaskWorkspaceBinding:
        del attempt_key, claims
        return self.binding

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        del binding
        return _CANONICAL_SEALED

    async def prepare(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        del binding
        assert sealed == _CANONICAL_SEALED
        return _CANONICAL_PREPARED

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        assert prepared == _CANONICAL_PREPARED
        return _CANONICAL_PROMOTION

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        assert prepared == _CANONICAL_PREPARED
        return _CANONICAL_PROMOTION


class _CanonicalJournalExecutor:
    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[ValidOutput]:
        del validated_input, scope
        return ExecutedAttemptResult(
            output=ValidOutput(value="done"),
            effects=(EffectIntent(kind=DELIVERY_KIND, payload=valid_delivery_payload()),),
            source_terminal_receipt=TerminalReceiptRef(
                identity_digest="a" * 64,
                receipt_digest="b" * 64,
            ),
        )


class _ConfigurableExecutor:
    def __init__(self) -> None:
        self.result: object | None = None
        self.calls = 0
        self.seen_scope: AuthorizedAttemptScope | None = None

    async def execute(self, validated_input: RunInput, scope: AuthorizedAttemptScope) -> object:
        del validated_input
        self.calls += 1
        self.seen_scope = scope
        (scope.workspace.write_root / "out.txt").write_bytes(b"done")
        assert self.result is not None
        return self.result


class _AuthorizationStoreView:
    def __init__(self, arbiter: ResourceArbiter) -> None:
        self._arbiter = arbiter

    async def is_active(self, attempt_key: AttemptKey) -> bool:
        return await self._arbiter.is_active(attempt_key)


class _KernelFixture:
    def __init__(
        self,
        *,
        kernel: AssuranceAttemptKernel,
        attempt_key: AttemptKey,
        resolved: Any,
        validated: RunInput,
        context: AttemptExecutionContext,
        executor: _ConfigurableExecutor,
        store: TaskWorkspaceStore,
    ) -> None:
        self.kernel = kernel
        self.journal = kernel.journal
        self.attempt_key = attempt_key
        self.resolved = resolved
        self.validated = validated
        self.context = context
        self.executor = executor
        self.store = store
        self.authorization_store = _AuthorizationStoreView(kernel.arbiter)

    def _ensure_success_result(self) -> None:
        if self.executor.result is None:
            self.executor.result = ExecutedAttemptResult(output=ValidOutput(value="done"))

    async def run_until_cut(self, name: str) -> None:
        self._ensure_success_result()

        def cut(cut_name: str) -> None:
            if cut_name == name:
                raise TransactionCrash(name)

        with pytest.raises(TransactionCrash, match=name):
            await self.kernel.execute_or_recover(
                self.attempt_key,
                self.resolved,
                self.validated,
                self.context,
                transaction_cut=cut,
            )

    async def crash_after(self, name: str) -> None:
        await self.run_until_cut(name)

    async def restart(self) -> object:
        self._ensure_success_result()
        return await self.kernel.execute_or_recover(
            self.attempt_key,
            self.resolved,
            self.validated,
            self.context,
            transaction_cut=None,
        )

    async def snapshot(self) -> object:
        loaded = await self.journal.load(self.attempt_key)
        assert loaded is not None
        return loaded

    async def install_impossible_release_state(self) -> None:
        self._ensure_success_result()
        result = await self.kernel.execute_or_recover(
            self.attempt_key,
            self.resolved,
            self.validated,
            self.context,
        )
        assert isinstance(result, CommittedTaskResult)
        granted = await self.kernel.arbiter.acquire(
            self.attempt_key,
            ResourceClaims(writes=("out.txt",)),
            fencing_token=self.context.fencing_token,
            validated_input=self.validated,
        )
        assert not isinstance(granted, PendingTaskResult)


def _effect_helpers() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "test_kernel_effects",
        Path(__file__).with_name("test_kernel_effects.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kernel_fixture(tmp_path: Path) -> Any:
    helpers = _effect_helpers()
    from graph_engine.effects.state import MemoryEffectState
    from graph_engine.plugin_api import EffectApplyResult

    handler = helpers.RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    effects, schemas = helpers.build_effect_registries(handler)
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    executor = _ConfigurableExecutor()
    resolved = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.execution.run.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.run",
            input_model=RunInput,
            output_model=ValidOutput,
            resources=ResourceClaims(writes=("out.txt",)),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=executor,
    )
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=graph_revision(),
        effects=effects,
        schemas=schemas,
        effect_state=MemoryEffectState(),
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
    assert DELIVERY_KIND in EXPECTED_EFFECT_KINDS
    try:
        yield _KernelFixture(
            kernel=kernel,
            attempt_key=key,
            resolved=resolved,
            validated=validated,
            context=context,
            executor=executor,
            store=store,
        )
    finally:
        store.close()


async def test_output_and_effect_intents_share_one_journal_revision(kernel_fixture) -> None:
    intent = EffectIntent(kind=DELIVERY_KIND, payload=valid_delivery_payload())
    kernel_fixture.executor.result = ExecutedAttemptResult(
        output=ValidOutput(value="done"),
        effects=(intent,),
        source_terminal_receipt=TerminalReceiptRef(
            identity_digest="a" * 64,
            receipt_digest="b" * 64,
        ),
    )
    await kernel_fixture.run_until_cut("after_observed_result")
    records = kernel_fixture.journal.records(kernel_fixture.attempt_key)
    assert [event.kind for event in records[-1].events] == [
        "activity_terminal_observed",
        "effect_intent_recorded",
    ]


async def test_terminal_state_with_active_grant_is_cleaned_before_replay(kernel_fixture) -> None:
    await kernel_fixture.crash_after("terminal_durable")
    assert await kernel_fixture.authorization_store.is_active(kernel_fixture.attempt_key)
    result = await kernel_fixture.restart()
    assert isinstance(result, CommittedTaskResult)
    assert not await kernel_fixture.authorization_store.is_active(kernel_fixture.attempt_key)
    assert (await kernel_fixture.snapshot()).released is True


async def test_released_grant_without_proof_appends_proof_on_restart(kernel_fixture) -> None:
    await kernel_fixture.crash_after("authorization_released")
    result = await kernel_fixture.restart()
    assert isinstance(result, CommittedTaskResult)
    assert (await kernel_fixture.snapshot()).released is True


async def test_release_proof_with_active_grant_fails_integrity(kernel_fixture) -> None:
    await kernel_fixture.install_impossible_release_state()
    with pytest.raises(AttemptIntegrityError, match="release proof"):
        await kernel_fixture.restart()


def _canonical_journal_scenario() -> tuple[
    AssuranceAttemptKernel,
    MemoryAttemptJournal,
    AttemptKey,
    Any,
    RunInput,
    AttemptExecutionContext,
]:
    helpers = _effect_helpers()
    handler = helpers.RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    effects, schemas = helpers.build_effect_registries(handler)
    executor = _CanonicalJournalExecutor()
    resolved = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.execution.run.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.run",
            input_model=RunInput,
            output_model=ValidOutput,
            resources=ResourceClaims(writes=("out.txt",)),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=executor,
    )
    journal = MemoryAttemptJournal()
    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=_CanonicalJournalWorkspace(),
        graph_revision=graph_revision(),
        effects=effects,
        schemas=schemas,
        effect_state=MemoryEffectState(),
    )
    validated = RunInput(change_id="chg-1")
    attempt_key = derive_attempt_key(
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
        attempt_key=attempt_key,
        fencing_token=4,
    )
    return kernel, journal, attempt_key, resolved, validated, context


def _canonical_journal_bytes(journal: MemoryAttemptJournal, attempt_key: AttemptKey) -> bytes:
    return canonical_json_bytes(
        [
            {
                "schema_version": ATTEMPT_JOURNAL_SCHEMA_VERSION,
                "record_digest": record.record_digest,
                "payload": record.canonical_projection(),
            }
            for record in journal.records(attempt_key)
        ]
    )


async def _run_canonical_journal_scenario(*, replay_prepared_state: bool) -> bytes:
    kernel, journal, attempt_key, resolved, validated, context = _canonical_journal_scenario()

    if replay_prepared_state:

        def cut(name: str) -> None:
            if name == "after_prepare_before_promotion":
                raise TransactionCrash(name)

        with pytest.raises(TransactionCrash, match="after_prepare_before_promotion"):
            await kernel.execute_or_recover(
                attempt_key,
                resolved,
                validated,
                context,
                transaction_cut=cut,
            )

    result = await kernel.execute_or_recover(
        attempt_key,
        resolved,
        validated,
        context,
        transaction_cut=None,
    )
    assert isinstance(result, CommittedTaskResult)
    return _canonical_journal_bytes(journal, attempt_key)


async def test_effectful_attempt_journal_bytes_match_phase_p_golden() -> None:
    fresh = await _run_canonical_journal_scenario(replay_prepared_state=False)
    replay = await _run_canonical_journal_scenario(replay_prepared_state=True)

    assert fresh == replay
    golden = Path(__file__).with_name("attempt-kernel-phase-p.golden.json").read_bytes()
    assert fresh == golden
