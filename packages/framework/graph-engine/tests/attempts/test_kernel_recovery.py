from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.checkpoint import AttemptPhase
from graph_engine.attempts.kernel import AssuranceAttemptKernel, AttemptIdentityDrift
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import CommittedTaskResult, PermanentTaskFailure
from graph_engine.attempts.resource_arbiter import ResourceArbiter, ResourceArbiterPort
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore, MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import (
    MemoryResourceAuthorizationStore,
    ResourceAuthorizationError,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import ResourceClaims, TaskWorkspaceBinding
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


class TransactionCrash(RuntimeError):
    pass


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None
        self.promotions = 0
        self.prepares = 0

    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims, *, seed_from: AttemptKey | None = None
    ) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims, seed_from=seed_from)
        return self.binding

    async def seal(self, binding: TaskWorkspaceBinding):
        return await self.inner.seal(binding)

    async def prepare(self, binding: TaskWorkspaceBinding, sealed):
        self.prepares += 1
        return await self.inner.prepare(binding, sealed)

    async def promote(self, prepared):
        self.promotions += 1
        return await self.inner.promote(prepared)

    async def recover_promotion(self, prepared):
        self.promotions += 1
        return await self.inner.recover_promotion(prepared)


class _WritingExecutor:
    def __init__(self, workspace: _RecordingWorkspace, *, files: dict[str, bytes] | None = None) -> None:
        self.workspace = workspace
        self.files = files if files is not None else {"out.txt": b"committed"}
        self.calls = 0

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[RunOutput]:
        del validated_input
        self.calls += 1
        for path, content in self.files.items():
            target = scope.workspace.write_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return ExecutedAttemptResult(output=RunOutput(status="ok"))


class _OtherOutput(BaseModel):
    unexpected: str


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


class _AdoptThenInvalidExecutor:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.kernel: AssuranceAttemptKernel | None = None
        self.key: AttemptKey | None = None
        self.calls = 0

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_OtherOutput]:
        del validated_input
        self.calls += 1
        assert self.kernel is not None and self.key is not None
        await self.kernel.arbiter.adopt(self.key, fencing_token=5)
        (scope.workspace.write_root / "out.txt").write_bytes(b"invalid")
        return ExecutedAttemptResult(output=_OtherOutput(unexpected="nope"))


class _CrashAfterObserveJournal:
    def __init__(self, inner: MemoryAttemptCheckpointStore) -> None:
        self.inner = inner
        self.crash_after_observe = True

    async def load(self, attempt_key: AttemptKey):
        return await self.inner.load(attempt_key)

    async def commit(self, checkpoint, *, expected_revision, fencing_token):
        snapshot = await self.inner.commit(
            checkpoint, expected_revision=expected_revision, fencing_token=fencing_token
        )
        if self.crash_after_observe and checkpoint.phase is AttemptPhase.COMMIT:
            self.crash_after_observe = False
            raise TransactionCrash("after activity terminal observed")
        return snapshot

    async def ensure_durable(self, attempt_key: AttemptKey) -> None:
        await self.inner.ensure_durable(attempt_key)


class _ReleaseOnceCrashArbiter:
    def __init__(self, inner: ResourceArbiterPort) -> None:
        self.inner = inner
        self.crash_on_release = True

    def claims_conflict(self, left: ResourceClaims, right: ResourceClaims) -> bool:
        return self.inner.claims_conflict(left, right)

    async def acquire(self, *args, **kwargs):
        return await self.inner.acquire(*args, **kwargs)

    async def adopt(self, *args, **kwargs):
        return await self.inner.adopt(*args, **kwargs)

    async def release(self, *args, **kwargs):
        if self.crash_on_release:
            self.crash_on_release = False
            raise TransactionCrash("after journaled release")
        return await self.inner.release(*args, **kwargs)

    async def assert_usable(self, *args, **kwargs):
        return await self.inner.assert_usable(*args, **kwargs)

    async def is_active(self, *args, **kwargs):
        return await self.inner.is_active(*args, **kwargs)


class _RecoverableExecutor:
    def __init__(self) -> None:
        self.workspace: _RecordingWorkspace | None = None
        self.calls = 0
        self.reconciles = 0
        self.crash_during_execute = True

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[RunOutput]:
        del validated_input
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_bytes(b"committed")
        if self.crash_during_execute:
            raise TransactionCrash("in-flight activity")
        return ExecutedAttemptResult(output=RunOutput(status="ok"))

    async def reconcile(
        self,
        validated_input: RunInput,
        scope: AuthorizedAttemptScope,
        activity: object,
    ) -> ExecutedAttemptResult[RunOutput]:
        del validated_input, activity
        self.reconciles += 1
        (scope.workspace.write_root / "out.txt").write_bytes(b"committed")
        return ExecutedAttemptResult(output=RunOutput(status="ok"))


def _revision() -> str:
    return canonical_digest({"revision": "kernel-recovery"})


def _contract(*, writes: tuple[str, ...] = ("out.txt",), validators: tuple[str, ...] = ()):
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(writes=writes),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=validators,
    )


def _build(
    tmp_path: Path,
    *,
    executor=None,
    writes: tuple[str, ...] = ("out.txt",),
    files: dict[str, bytes] | None = None,
    transaction_cut=None,
    journal: AttemptCheckpointStore | None = None,
    authorization_store: MemoryResourceAuthorizationStore | None = None,
    fencing_token: int = 4,
    graph_revision: str | None = None,
):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = executor if executor is not None else _WritingExecutor(workspace, files=files)
    writer.workspace = workspace
    resolved = resolve_contract(_contract(writes=writes), executor=writer)
    revision = graph_revision or _revision()
    owned_journal = journal if journal is not None else MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=owned_journal,
        arbiter=ResourceArbiter(
            authorization_store if authorization_store is not None else MemoryResourceAuthorizationStore()
        ),
        workspace=workspace,
        graph_revision=revision,
        validators={},
        transaction_cut=transaction_cut,
    )
    validated = RunInput(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=revision,
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
        fencing_token=fencing_token,
    )
    return kernel, key, resolved, validated, context, writer, workspace, project, store, owned_journal


@pytest.mark.parametrize(
    "fault",
    [
        "after_observed_result",
        "before_durable_prepare",
        "after_prepare_before_promotion",
        "during_multi_file_promotion",
        "after_promotion_before_receipt",
        "terminal_durable",
        "authorization_released",
        "release_proof",
        "after_receipt_before_graph_checkpoint",
    ],
)
async def test_crash_windows_replay_same_receipt_without_repeating_mutation(
    tmp_path: Path, fault: str
) -> None:
    writes = ("a.txt", "b.txt") if fault == "during_multi_file_promotion" else ("out.txt",)
    files = (
        {"a.txt": b"one", "b.txt": b"two"}
        if fault == "during_multi_file_promotion"
        else {"out.txt": b"committed"}
    )
    hits = {"count": 0}

    def cut(name: str) -> None:
        if name == fault:
            hits["count"] += 1
            raise TransactionCrash(fault)

    kernel, key, resolved, validated, context, executor, workspace, project, store, _journal = _build(
        tmp_path,
        writes=writes,
        files=files,
        transaction_cut=None if fault == "after_receipt_before_graph_checkpoint" else cut,
    )
    first: CommittedTaskResult[RunOutput] | None = None
    try:
        if fault == "after_receipt_before_graph_checkpoint":
            first_result = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(first_result, CommittedTaskResult)
            first = first_result
        else:
            with pytest.raises(TransactionCrash, match=fault):
                await kernel.execute_or_recover(key, resolved, validated, context)

        replay = await kernel.execute_or_recover(
            key,
            resolved,
            validated,
            context,
            transaction_cut=None,
        )
        assert isinstance(replay, CommittedTaskResult)
        if first is not None:
            assert replay.receipt == first.receipt
        if fault != "before_durable_prepare":
            assert executor.calls == 1
        if fault == "during_multi_file_promotion":
            assert workspace.promotions == 2
        else:
            assert workspace.promotions == 1
        if writes == ("out.txt",):
            assert (project / "out.txt").read_bytes() == b"committed"
        else:
            assert (project / "a.txt").read_bytes() == b"one"
            assert (project / "b.txt").read_bytes() == b"two"
        second = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(second, CommittedTaskResult)
        assert second.receipt == replay.receipt
        assert executor.calls == 1
        assert workspace.promotions == (2 if fault == "during_multi_file_promotion" else 1)
        if writes == ("out.txt",):
            assert (project / "out.txt").read_bytes() == b"committed"
    finally:
        store.close()


async def test_replay_rejects_input_contract_and_revision_drift(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, _executor, _workspace, _project, store, journal = _build(
        tmp_path
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, CommittedTaskResult)
        with pytest.raises(AttemptIdentityDrift, match="input"):
            await kernel.execute_or_recover(key, resolved, RunInput(change_id="other"), context)
        other_contract = resolve_contract(
            TaskAttemptContract(
                contract_id="assurance.execution.rerun.v1",
                owner_id="assurance.execution",
                handler_id="assurance.execution.rerun",
                input_model=RunInput,
                output_model=RunOutput,
                resources=ResourceClaims(writes=("out.txt",)),
                retry=AttemptRetryPolicy(max_attempts=1),
                timeout=AttemptTimeoutPolicy(seconds=60),
                validators=(),
            ),
            executor=_WritingExecutor(_RecordingWorkspace(TaskWorkspaceProvider(store))),
        )
        with pytest.raises(AttemptIdentityDrift, match="contract"):
            await kernel.execute_or_recover(key, other_contract, validated, context)
        drifted = AssuranceAttemptKernel(
            checkpoints=journal,
            arbiter=kernel.arbiter,
            workspace=kernel.workspace,
            graph_revision=canonical_digest({"revision": "other"}),
            validators={},
        )
        with pytest.raises(AttemptIdentityDrift, match="revision"):
            await drifted.execute_or_recover(key, resolved, validated, context)
    finally:
        store.close()


async def test_in_flight_recoverable_handler_is_adopted_with_same_key(tmp_path: Path) -> None:
    recoverable = _RecoverableExecutor()
    kernel, key, resolved, validated, context, executor, _workspace, project, store, journal = _build(
        tmp_path,
        executor=recoverable,
    )
    assert isinstance(executor, _RecoverableExecutor)
    try:
        with pytest.raises(TransactionCrash, match="in-flight"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        snapshot = await journal.load(key)
        assert snapshot is not None
        assert snapshot.activity_state == "prepared"
        assert snapshot.phase is AttemptPhase.RECONCILE
        recoverable.crash_during_execute = False
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        assert recoverable.reconciles == 1
        assert recoverable.calls == 1
        assert (project / "out.txt").read_bytes() == b"committed"
    finally:
        store.close()


async def test_fence_is_checked_at_irreversible_boundaries(tmp_path: Path) -> None:
    seen: list[str] = []

    def cut(name: str) -> None:
        if name.startswith("fence:"):
            seen.append(name)

    kernel, key, resolved, validated, context, _executor, _workspace, _project, store, _journal = _build(
        tmp_path, transaction_cut=cut
    )
    try:
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        assert seen == [
            "fence:external_dispatch",
            "fence:durable_prepare",
            "fence:promotion",
            "fence:terminal_receipt",
            "fence:resource_release",
        ]
    finally:
        store.close()


async def test_stale_runner_can_observe_but_cannot_commit_after_lease_loss(tmp_path: Path) -> None:
    journal = MemoryAttemptCheckpointStore()
    authorization = MemoryResourceAuthorizationStore()
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    executor = _RecoverableExecutor()
    executor.workspace = workspace
    resolved = resolve_contract(_contract(), executor=executor)
    old = AssuranceAttemptKernel(
        checkpoints=journal,
        arbiter=ResourceArbiter(authorization),
        workspace=workspace,
        graph_revision=_revision(),
        validators={},
    )
    validated = RunInput(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=_revision(),
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    old_context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=4,
    )
    new_context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=5,
    )
    try:
        with pytest.raises(TransactionCrash, match="in-flight"):
            await old.execute_or_recover(key, resolved, validated, old_context)
        executor.crash_during_execute = False
        observed = await journal.load(key)
        assert observed is not None

        def crash_after_adoption(name: str) -> None:
            if name == "fence:external_dispatch":
                raise TransactionCrash("after adoption")

        with pytest.raises(TransactionCrash, match="after adoption"):
            await old.execute_or_recover(
                key,
                resolved,
                validated,
                new_context,
                transaction_cut=crash_after_adoption,
            )
        with pytest.raises(StaleFencingToken):
            await old.execute_or_recover(key, resolved, validated, old_context)
        result = await old.execute_or_recover(key, resolved, validated, new_context)
        assert isinstance(result, CommittedTaskResult)
        assert executor.reconciles == 1
        assert (project / "out.txt").read_bytes() == b"committed"
    finally:
        store.close()


async def test_invalid_output_is_not_journaled_as_observed(tmp_path: Path) -> None:
    journal = _CrashAfterObserveJournal(MemoryAttemptCheckpointStore())
    kernel, key, resolved, validated, context, executor, _workspace, _project, store, owned_journal = _build(
        tmp_path,
        executor=_InvalidOutputExecutor(),
        journal=journal,
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, PermanentTaskFailure)
        assert first.kind == "invalid_output"
        snapshot = await owned_journal.load(key)
        assert snapshot is not None
        assert snapshot.activity_state != "terminal_observed"
        assert snapshot.terminal is not None
        assert snapshot.released is True
        with pytest.raises(ResourceAuthorizationError, match="no active authorization"):
            await kernel.arbiter.assert_usable(key, fencing_token=context.fencing_token)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert replay == first
        assert executor.calls == 1
    finally:
        store.close()


async def test_fail_closed_after_lease_loss_cannot_persist_terminal(tmp_path: Path) -> None:
    executor = _AdoptThenInvalidExecutor()
    kernel, key, resolved, validated, context, _writer, _workspace, _project, store, journal = _build(
        tmp_path,
        executor=executor,
        fencing_token=4,
    )
    executor.kernel = kernel
    executor.key = key
    try:
        with pytest.raises(StaleFencingToken):
            await kernel.execute_or_recover(key, resolved, validated, context)
        snapshot = await journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is None
        successor = context.model_copy(update={"fencing_token": 5})
        result = await kernel.execute_or_recover(key, resolved, validated, successor)
        assert isinstance(result, PermanentTaskFailure)
        assert result.kind == "internal"
        assert "cannot be adopted" in result.message
        snapshot = await journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is not None
        assert snapshot.released is True
        assert executor.calls == 1
    finally:
        store.close()


async def test_terminal_replay_releases_held_grant(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, executor, _workspace, _project, store, journal = _build(
        tmp_path
    )
    kernel.arbiter = _ReleaseOnceCrashArbiter(kernel.arbiter)
    try:
        with pytest.raises(TransactionCrash, match="after journaled release"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        snapshot = await journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is not None
        assert snapshot.released is False
        await kernel.arbiter.assert_usable(key, fencing_token=context.fencing_token)
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        snapshot = await journal.load(key)
        assert snapshot is not None
        assert snapshot.released is True
        with pytest.raises(ResourceAuthorizationError, match="no active authorization"):
            await kernel.arbiter.assert_usable(key, fencing_token=context.fencing_token)
        assert executor.calls == 1
    finally:
        store.close()


async def test_post_promotion_restart_uses_artifacts_without_executing_business_code(tmp_path: Path) -> None:
    def cut(name: str) -> None:
        if name == "after_promotion_before_receipt":
            raise TransactionCrash(name)

    kernel, key, resolved, validated, context, writer, workspace, project, store, journal = _build(
        tmp_path, transaction_cut=cut
    )
    try:
        with pytest.raises(TransactionCrash, match="after_promotion_before_receipt"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        promoted = await journal.load(key)
        assert promoted is not None and promoted.terminal is None
        assert (project / "out.txt").read_bytes() == b"committed"

        restarted = AssuranceAttemptKernel(
            checkpoints=journal, arbiter=kernel.arbiter, workspace=workspace, graph_revision=_revision()
        )
        replay = await restarted.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, CommittedTaskResult)
        assert replay.receipt.receipt_digest == promoted.promotion_receipt_digest
        assert [artifact.path for artifact in replay.committed_artifacts] == ["out.txt"]
        assert writer.calls == 1
        assert workspace.promotions == 1
        again = await restarted.execute_or_recover(key, resolved, validated, context)
        assert again == replay
        assert writer.calls == 1
        assert workspace.promotions == 1
    finally:
        store.close()


async def test_production_regenerates_after_file_promotion_without_graph_completion(tmp_path: Path) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    from graph_engine.attempts.node_factory import AttemptNodeFactory

    def cut(name):
        if name == "after_promotion_before_receipt":
            raise TransactionCrash(name)

    kernel, key, resolved, validated, context, writer, workspace, project, store, journal = _build(
        tmp_path, transaction_cut=cut
    )
    resolved = resolve_contract(
        replace(resolved.contract, retry=AttemptRetryPolicy(max_attempts=3)), executor=writer
    )
    runtime = SimpleNamespace(
        invocation_id=context.invocation_id,
        public_entrypoint=context.public_entrypoint,
        revision_id=_revision(),
        fencing_token=context.fencing_token,
        attempt_kernel=kernel,
    )

    def node_for(selected_kernel):
        return AttemptNodeFactory(checkpoints=journal, kernel=selected_kernel, regenerate=True).attempt(
            resolved,
            semantic_node_id="execution.run",
            activation=lambda state: BusinessActivation.one_shot(),
            select=lambda state: validated,
            publish=lambda state, output, receipt: {"output": output},
        )

    try:
        with pytest.raises(TransactionCrash):
            await node_for(kernel)({}, runtime)
        assert (project / "out.txt").read_bytes() == b"committed"
        old = await journal.load(key)
        assert old.promotion_receipt_digest is not None
        # Represents scoped release after the previous execution is proven gone.
        await kernel.arbiter.release(key, fencing_token=context.fencing_token)
        writer.files = {"out.txt": b"regenerated"}
        restarted = AssuranceAttemptKernel(
            checkpoints=journal, arbiter=kernel.arbiter, workspace=workspace, graph_revision=_revision()
        )
        runtime.attempt_kernel = restarted
        await node_for(restarted)({}, runtime)
        assert writer.calls == 2
        assert (project / "out.txt").read_bytes() == b"regenerated"
        assert (await journal.load(key)).promotion_receipt_digest == old.promotion_receipt_digest
    finally:
        store.close()
