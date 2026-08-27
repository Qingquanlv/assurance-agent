from __future__ import annotations

from bootstrap_fixtures import synthetic_invocation_started
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ContributionAuthority,
    EffectRegistry,
    ExecutableAuthority,
    SchemaRegistry,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginContribution,
    PluginDescriptor,
    SchemaContribution,
    StagedWriteSet,
    TaskFailure,
    TaskWorkspaceIdentity,
)
from graph_engine.runtime.events import (
    EffectApplyStarted,
    EffectIntentCommitted,
    EffectReceiptRecorded,
    GraphStarted,
    NodeActivated,
    TaskAttemptCommittedEffectFailed,
    TaskAttemptStarted,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskPromotionCompleted,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events


_KIND = "test.effects.audit"
_PAYLOAD: dict[str, int] = {"n": 1}
_EFFECT_ID = "effect-1"
_LOCK = "a" * 64
_KEY = canonical_digest(
    {
        "lock_digest": _LOCK,
        "effect_id": _EFFECT_ID,
        "kind": _KIND,
        "payload_digest": canonical_digest(_PAYLOAD),
    }
)
_INTENT_SCHEMA = (
    b'{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],"additionalProperties":false}'
)
_RECEIPT_SCHEMA = (
    b'{"type":"object","properties":{"remote_id":{"type":"string"}},"required":["remote_id"],'
    b'"additionalProperties":false}'
)
_TRANSIENT = EffectApplyResult(
    status="transient",
    failure=TaskFailure(kind="transient", message="busy"),
)
_PERMANENT = EffectApplyResult(
    status="permanent",
    failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
)


class RecordingEffectHandler:
    def __init__(
        self,
        *,
        apply_result: EffectApplyResult | Sequence[EffectApplyResult] | None = None,
        reconcile_result: EffectReconcileResult | Sequence[EffectReconcileResult] | None = None,
        apply_error: BaseException | None = None,
        reconcile_error: BaseException | None = None,
    ) -> None:
        self._apply_keys: list[str] = []
        self._reconcile_keys: list[str] = []
        self._apply_error = apply_error
        self._reconcile_error = reconcile_error
        self._apply_results = _queued(apply_result)
        self._reconcile_results = _queued(reconcile_result)

    @property
    def apply_keys(self) -> tuple[str, ...]:
        return tuple(self._apply_keys)

    @property
    def reconcile_keys(self) -> tuple[str, ...]:
        return tuple(self._reconcile_keys)

    async def apply(self, _intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        self._apply_keys.append(idempotency_key)
        if self._apply_error is not None:
            raise self._apply_error
        return _take(self._apply_results, "apply")

    async def reconcile(self, _intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        self._reconcile_keys.append(idempotency_key)
        if self._reconcile_error is not None:
            raise self._reconcile_error
        return _take(self._reconcile_results, "reconcile")


def _queued(
    value: EffectApplyResult | EffectReconcileResult | Sequence[object] | None,
) -> list[object] | object | None:
    if value is None or isinstance(value, EffectApplyResult | EffectReconcileResult):
        return value
    return list(value)


def _take(stored: list[object] | object | None, label: str) -> object:
    if stored is None:
        raise AssertionError(f"{label} result is not configured")
    if isinstance(stored, list):
        if not stored:
            raise AssertionError(f"no remaining {label} result")
        return stored.pop(0)
    return stored


def _source(plugin_id: str) -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=Path(f"/sources/{plugin_id}"),
            distribution=plugin_id.replace(".", "-"),
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=f"{plugin_id.replace('.', '_')}:provider",
            declaration_path=f"{plugin_id.replace('.', '_')}/plugin-declaration.json",
            import_roots=("",),
            plugin_id=plugin_id,
            plugin_version="1.0.0",
        ),
        (),
    )


def _effect_key(effect_id: str, payload: dict[str, int]) -> str:
    return canonical_digest(
        {
            "lock_digest": _LOCK,
            "effect_id": effect_id,
            "kind": _KIND,
            "payload_digest": canonical_digest(payload),
        }
    )


def _workspace_identity(task_id: str) -> TaskWorkspaceIdentity:
    payload = {
        "task_id": task_id,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": canonical_digest({"task_id": task_id, "kind": "write-root"}),
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def _staged_write_set(task_id: str) -> StagedWriteSet:
    identity = _workspace_identity(task_id)
    payload = {"identity_digest": identity.identity_digest, "files": []}
    return StagedWriteSet(
        identity_digest=identity.identity_digest,
        files=(),
        staged_digest=canonical_digest(payload),
    )


def _effect_registries(
    handler: RecordingEffectHandler,
    *,
    kinds: tuple[str, ...] = (_KIND,),
    policy: EffectPolicy | None = None,
    receipt_schema: bytes = _RECEIPT_SCHEMA,
) -> tuple[EffectRegistry, SchemaRegistry]:
    owner_id = "test.effects"
    source = _source(owner_id)
    selected_policy = policy or EffectPolicy(max_attempts=3, timeout_seconds=30, backoff_seconds=0)
    contribution = PluginContribution(
        schemas=(
            SchemaContribution(f"{owner_id}.intent", "application/schema+json", _INTENT_SCHEMA),
            SchemaContribution(f"{owner_id}.receipt", "application/schema+json", receipt_schema),
        ),
        effects=tuple(
            EffectRegistration(
                kind=kind,
                intent_schema_id=f"{owner_id}.intent",
                receipt_schema_id=f"{owner_id}.receipt",
                handler=handler,
                policy=selected_policy,
            )
            for kind in kinds
        ),
    )
    source_key = SourceKey(SourceRole.PLUGIN, owner_id)
    proofs = [
        ExecutableProvenance.create(
            kind=kind,
            registry_id=registration.kind,
            owner_id=owner_id,
            source_key=source_key,
            source_digest=source.digest,
            module=ExecutableModuleProvenance(
                module_name="test_effects.implementation",
                standard_loader=StandardLoader.SOURCE,
                standard_is_package=False,
                relative_origin="implementation.py",
                authenticated_locations=(),
                physical_sha256="0" * 64,
                source_digest=source.digest,
            ),
            callable_path="test.effects.implementation:Handler.apply",
            binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
        )
        for registration in contribution.effects
        for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
    ]
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=owner_id,
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=(),
        commit_validators=(),
        schemas=tuple(item.schema_id for item in contribution.schemas),
        effects=tuple(item.kind for item in contribution.effects),
    )
    executable_objects = {
        (kind, registration.kind): registration.handler
        for registration in contribution.effects
        for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
    }
    ordered_proofs = tuple(sorted(proofs, key=lambda item: (item.registry_id, item.kind.value)))
    authority_set = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id=owner_id,
        source_key=source_key,
        source_digest=source.digest,
        contribution=contribution,
        authorities=tuple(
            ExecutableAuthority(
                executable=executable_objects[(proof.kind, proof.registry_id)],
                function=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[proof.kind.slot],
                bound_self=executable_objects[(proof.kind, proof.registry_id)],
                descriptor=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[
                    proof.kind.slot
                ],
                provenance=proof,
            )
            for proof in ordered_proofs
        ),
    )
    registries = _build_registries(
        (source,),
        (
            AuthenticatedContribution(
                owner_id=owner_id,
                source_key=source_key,
                source_digest=source.digest,
                descriptor=descriptor,
                contribution=contribution,
                executables=ordered_proofs,
                authority=authority_set,
            ),
        ),
        (owner_id,),
    )
    return registries.effects, registries.schemas


def _task_events(
    *,
    task_id: str,
    activation_id: str,
    node_id: str,
    token_id: str,
    effects: tuple[tuple[str, dict[str, int]], ...],
) -> list[object]:
    effect_ids = tuple(effect_id for effect_id, _payload in effects)
    workspace = _workspace_identity(task_id)
    staged = _staged_write_set(task_id)
    events: list[object] = [
        TokenOffered(
            token_id=token_id,
            graph_instance_id="root",
            source=None,
            target=node_id,
            payload=None,
        ),
        TokenConsumed(token_id=token_id, graph_instance_id="root", node_id=node_id),
        NodeActivated(
            activation_id=activation_id,
            graph_instance_id="root",
            node_id=node_id,
            token_ids=(token_id,),
        ),
        TaskAttemptStarted(activation_id=activation_id, attempt=1, lease_expires_at="11"),
        TaskLeaseAcquired(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
        TaskCommitPrepared(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            output={"ok": True},
            workspace_identity=workspace,
            staged_write_set=staged,
            staged_write_set_digest=staged.staged_digest,
            effect_ids=effect_ids,
        ),
    ]
    events.extend(
        EffectIntentCommitted(
            effect_id=effect_id,
            activation_id=activation_id,
            attempt=1,
            index=index,
            effect_kind=_KIND,
            payload=dict(payload),
            idempotency_key=_effect_key(effect_id, payload),
        )
        for index, (effect_id, payload) in enumerate(effects)
    )
    events.append(
        TaskPromotionCompleted(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        )
    )
    return events


def _effect_executor(
    tmp_path: Path,
    *,
    handler: RecordingEffectHandler,
    state: str = "committed",
    policy: EffectPolicy | None = None,
    effects: tuple[tuple[str, dict[str, int]], ...] = ((_EFFECT_ID, _PAYLOAD),),
    tasks: tuple[tuple[str, str], ...] = (("task-1", "a1"),),
    receipt_schema: bytes = _RECEIPT_SCHEMA,
):
    from graph_engine.runtime.effects import EffectExecutor

    effects_registry, schemas = _effect_registries(
        handler,
        policy=policy,
        receipt_schema=receipt_schema,
    )
    ledger = Ledger(tmp_path / "ledger")
    events: list[object] = [
        synthetic_invocation_started(lock_digest=_LOCK),
        GraphStarted(graph_instance_id="root", graph_id="root"),
    ]
    for index, (task_id, activation_id) in enumerate(tasks):
        task_effects = effects if len(tasks) == 1 else ((f"effect-{task_id}", {"n": index + 1}),)
        events.extend(
            _task_events(
                task_id=task_id,
                activation_id=activation_id,
                node_id=f"node-{index}",
                token_id=f"tok-{index}",
                effects=task_effects,
            )
        )
    if state == "applying":
        events.append(EffectApplyStarted(effect_id=effects[0][0], apply_attempt=1))
    elif state == "receipts":
        for index, (effect_id, _payload) in enumerate(effects):
            events.append(EffectApplyStarted(effect_id=effect_id, apply_attempt=1))
            events.append(
                EffectReceiptRecorded(
                    effect_id=effect_id,
                    apply_attempt=1,
                    receipt={"remote_id": f"r{index + 1}"},
                )
            )
    elif state != "committed":
        raise AssertionError(f"unknown fixture state: {state}")
    ledger.append_batch(cast(tuple[object, ...], tuple(events)), expected_next_seq=1)
    projection = fold_events(ledger.read_all())
    executor = EffectExecutor(effects_registry, schemas, ledger)
    return executor, ledger, projection


def test_executor_applies_committed_effect_and_records_receipt(tmp_path: Path) -> None:
    from graph_engine.runtime.effects import EffectExecutor

    del EffectExecutor
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="committed")
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    assert handler.apply_keys == (_KEY,)
    assert handler.reconcile_keys == ()
    assert tuple(event.event.kind for event in ledger.read_all()[-2:]) == (
        "effect_apply_started",
        "effect_receipt_recorded",
    )


def test_executor_reconciles_after_apply_started_without_blind_reapply(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(reconcile_result=EffectReconcileResult.applied({"remote_id": "r1"}))
    executor, _ledger, projection = _effect_executor(tmp_path, handler=handler, state="applying")
    asyncio.run(executor.settle_next(projection))
    assert handler.apply_keys == ()
    assert handler.reconcile_keys == (_KEY,)


def test_executor_applies_after_reconcile_reports_not_applied(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(
        apply_result=EffectApplyResult.applied({"remote_id": "r1"}),
        reconcile_result=EffectReconcileResult(status="not_applied"),
    )
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="applying")
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    assert handler.reconcile_keys == (_KEY,)
    assert handler.apply_keys == (_KEY,)
    assert ledger.read_all()[-1].event.kind == "effect_receipt_recorded"


def test_executor_does_not_apply_when_reconcile_is_pending(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(reconcile_result=EffectReconcileResult(status="pending"))
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="applying")
    before = len(ledger.read_all())
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is False
    assert settlement.pending is True
    assert handler.apply_keys == ()
    assert handler.reconcile_keys == (_KEY,)
    assert len(ledger.read_all()) == before


def test_executor_retries_transient_then_not_applied_without_burning_start_slot(
    tmp_path: Path,
) -> None:
    handler = RecordingEffectHandler(
        apply_result=_TRANSIENT,
        reconcile_result=EffectReconcileResult(status="not_applied"),
    )
    policy = EffectPolicy(max_attempts=2, timeout_seconds=30, backoff_seconds=0)
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        policy=policy,
    )

    first = asyncio.run(executor.settle_next(projection))
    assert first.progressed is True
    assert handler.apply_keys == (_KEY,)
    assert handler.reconcile_keys == ()
    assert sum(1 for event in ledger.read_all() if event.event.kind == "effect_apply_started") == 1

    second = asyncio.run(executor.settle_next(fold_events(ledger.read_all())))
    assert second.progressed is True
    assert handler.apply_keys == (_KEY, _KEY)
    assert handler.reconcile_keys == (_KEY,)
    assert sum(1 for event in ledger.read_all() if event.event.kind == "effect_apply_started") == 2
    assert all(event.event.kind != "task_attempt_failed" for event in ledger.read_all())

    third = asyncio.run(executor.settle_next(fold_events(ledger.read_all())))
    assert third.progressed is True
    assert handler.apply_keys == (_KEY, _KEY)
    failed = ledger.read_all()[-1].event
    assert isinstance(failed, TaskAttemptCommittedEffectFailed)
    assert failed.failure.retryable is False
    assert failed.failure.kind == "external_effect"
    folded = fold_events(ledger.read_all())
    attempt = folded.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == "c" * 64


def test_executor_retries_transient_apply_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    handler = RecordingEffectHandler(apply_result=_TRANSIENT)
    policy = EffectPolicy(max_attempts=3, timeout_seconds=30, backoff_seconds=0.25)
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        policy=policy,
    )
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    assert handler.apply_keys == (_KEY,)
    assert slept == [0.25]
    assert ledger.read_all()[-1].event.kind == "effect_apply_started"
    assert all(event.event.kind != "effect_receipt_recorded" for event in ledger.read_all())


def test_executor_rejects_invalid_receipt_without_publication(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"n": 1}))
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="committed")
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    kinds = tuple(event.event.kind for event in ledger.read_all())
    assert "effect_receipt_recorded" not in kinds
    failed = ledger.read_all()[-1].event
    assert isinstance(failed, TaskAttemptCommittedEffectFailed)
    assert failed.failure.retryable is False
    assert failed.failure.kind == "invalid_output"


def test_executor_treats_handler_exception_as_ambiguous(tmp_path: Path) -> None:
    from graph_engine.runtime.effects import EffectPublicationIndeterminate

    handler = RecordingEffectHandler(apply_error=RuntimeError("handler crashed"))
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="committed")
    with pytest.raises(EffectPublicationIndeterminate):
        asyncio.run(executor.settle_next(projection))
    assert handler.apply_keys == (_KEY,)
    kinds = tuple(event.event.kind for event in ledger.read_all())
    assert kinds[-1] == "effect_apply_started"
    assert "effect_receipt_recorded" not in kinds
    assert "task_attempt_failed" not in kinds


@pytest.mark.parametrize(
    ("apply_result", "reconcile_result", "policy"),
    [
        (_PERMANENT, None, None),
        (EffectApplyResult.applied({"n": 1}), None, None),
        (
            _TRANSIENT,
            EffectReconcileResult(status="not_applied"),
            EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0),
        ),
    ],
)
def test_executor_non_last_effect_failure_marks_remaining_intents_failed(
    tmp_path: Path,
    apply_result: EffectApplyResult,
    reconcile_result: EffectReconcileResult | None,
    policy: EffectPolicy | None,
) -> None:
    from graph_engine.runtime.effects import needs_settlement

    handler = RecordingEffectHandler(apply_result=apply_result, reconcile_result=reconcile_result)
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        policy=policy,
        effects=(("effect-1", {"n": 1}), ("effect-2", {"n": 2})),
    )
    asyncio.run(executor.settle_next(projection))
    if reconcile_result is not None:
        asyncio.run(executor.settle_next(fold_events(ledger.read_all())))
    folded = fold_events(ledger.read_all())
    assert tuple(item.status for item in folded.effects) == ("permanently_failed", "permanently_failed")
    attempt = folded.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == "c" * 64
    assert needs_settlement(folded) is False
    assert handler.apply_keys == (_effect_key("effect-1", {"n": 1}),)
    assert handler.reconcile_keys == ((_effect_key("effect-1", {"n": 1}),) if reconcile_result else ())
    failed = next(
        event.event
        for event in ledger.read_all()
        if isinstance(event.event, TaskAttemptCommittedEffectFailed)
    )
    assert failed.failure.retryable is False


def test_executor_publishes_permanent_failure_without_rolling_back_head(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(apply_result=_PERMANENT)
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="committed")
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    kinds = tuple(event.event.kind for event in ledger.read_all())
    assert kinds[-1] == "task_attempt_committed_effect_failed"
    assert "effect_receipt_recorded" not in kinds
    assert "task_attempt_failed" not in kinds
    committed_failure = ledger.read_all()[-1].event
    assert committed_failure.failure.retryable is False
    assert committed_failure.failure.kind == "external_effect"
    assert committed_failure.staged_write_set_digest
    assert committed_failure.promotion_receipt_digest == "c" * 64
    folded = fold_events(ledger.read_all())
    attempt = folded.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == "c" * 64


def test_executor_exhausts_policy_as_non_retryable_failure(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(
        apply_result=_TRANSIENT,
        reconcile_result=EffectReconcileResult(status="not_applied"),
    )
    policy = EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0)
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        policy=policy,
    )
    first = asyncio.run(executor.settle_next(projection))
    assert first.progressed is True
    second = asyncio.run(executor.settle_next(fold_events(ledger.read_all())))
    assert second.progressed is True
    failed = ledger.read_all()[-1].event
    assert isinstance(failed, TaskAttemptCommittedEffectFailed)
    assert failed.failure.retryable is False
    folded = fold_events(ledger.read_all())
    attempt = folded.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == "c" * 64
    assert handler.apply_keys == (_KEY,)


def test_executor_applies_multiple_effects_in_serial_index_order(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    payloads = (("zzz", {"n": 1}), ("aaa", {"n": 2}))
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        effects=payloads,
    )
    first = asyncio.run(executor.settle_next(projection))
    assert first.progressed is True
    assert handler.apply_keys == (_effect_key("zzz", {"n": 1}),)
    kinds = tuple(event.event.kind for event in ledger.read_all())
    assert kinds.count("effect_apply_started") == 1
    assert kinds.count("effect_receipt_recorded") == 1
    receipt = ledger.read_all()[-1].event
    assert isinstance(receipt, EffectReceiptRecorded)
    assert receipt.effect_id == "zzz"

    second_projection = fold_events(ledger.read_all())
    second = asyncio.run(executor.settle_next(second_projection))
    assert second.progressed is True
    assert handler.apply_keys == (
        _effect_key("zzz", {"n": 1}),
        _effect_key("aaa", {"n": 2}),
    )


def test_executor_selects_multiple_tasks_in_canonical_order(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="committed",
        tasks=(("task-b", "b1"), ("task-a", "a1")),
    )
    asyncio.run(executor.settle_next(projection))
    started = next(event.event for event in ledger.read_all() if isinstance(event.event, EffectApplyStarted))
    selected = next(
        item for item in fold_events(ledger.read_all()).effects if item.effect_id == started.effect_id
    )
    assert selected.task_id == "task-a"
    assert started.effect_id == selected.effect_id


def test_public_executor_publishes_digest_bound_task_success_after_all_receipts(
    tmp_path: Path,
) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    executor, ledger, projection = _effect_executor(
        tmp_path,
        handler=handler,
        state="receipts",
        effects=(("effect-1", {"n": 1}), ("effect-2", {"n": 2})),
    )
    attempt = projection.activations[-1].attempts[-1]
    prepared = attempt.prepared_commit
    assert prepared is not None
    assert prepared.promotion_receipt_digest == "c" * 64

    settlement = asyncio.run(executor.settle_next(projection))

    assert settlement.progressed is True
    assert handler.apply_keys == ()
    assert handler.reconcile_keys == ()
    succeeded = ledger.read_all()[-1].event
    assert isinstance(succeeded, TaskAttemptSucceeded)
    assert succeeded.output == {"ok": True}
    assert succeeded.staged_write_set_digest == prepared.staged_write_set_digest
    assert succeeded.promotion_receipt_digest == prepared.promotion_receipt_digest
    folded = fold_events(ledger.read_all())
    assert folded.activations[-1].attempts[-1].status == "succeeded"
    assert tuple(item.status for item in folded.effects) == ("applied", "applied")
