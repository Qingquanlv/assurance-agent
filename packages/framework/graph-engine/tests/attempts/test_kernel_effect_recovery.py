from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graph_engine.attempts.events import EffectApplied, EffectIntentRecorded
from graph_engine.attempts.resolutions import CommittedTaskResult, IndeterminateTaskResult
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS, effect_idempotency_key
from graph_engine.plugin_api import EffectApplyResult, EffectPolicy, EffectReconcileResult, TaskFailure


def _load_effect_helpers() -> object:
    spec = importlib.util.spec_from_file_location(
        "test_kernel_effects",
        Path(__file__).with_name("test_kernel_effects.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HELPERS = _load_effect_helpers()
RecordingEffectHandler = _HELPERS.RecordingEffectHandler
make_effect_kernel = _HELPERS.make_effect_kernel


class TransactionCrash(RuntimeError):
    pass


class _CrashAfterEffectJournal:
    def __init__(self, inner: object, *, crash_on: type[object]) -> None:
        self.inner = inner
        self.crash_on = crash_on
        self.crashed = False

    async def load(self, attempt_key):
        return await self.inner.load(attempt_key)  # type: ignore[no-any-return]

    async def append(self, attempt_key, events, *, expected_revision, fencing_token):
        snapshot = await self.inner.append(  # type: ignore[misc]
            attempt_key,
            events,
            expected_revision=expected_revision,
            fencing_token=fencing_token,
        )
        if not self.crashed and any(isinstance(event, self.crash_on) for event in events):
            self.crashed = True
            raise TransactionCrash(self.crash_on.__name__)
        return snapshot

    async def ensure_durable(self, attempt_key) -> None:
        await self.inner.ensure_durable(attempt_key)  # type: ignore[misc]


@pytest.mark.parametrize("kind", sorted(EXPECTED_EFFECT_KINDS))
@pytest.mark.parametrize(
    "fault",
    [
        "after_promotion_before_effect",
        "after_effect_apply_before_receipt",
    ],
)
async def test_crash_after_promotion_reconciles_without_repeating_promotion(
    tmp_path: Path, kind: str, fault: str
) -> None:
    hits = {"count": 0}

    def cut(name: str) -> None:
        if name == "after_promotion_before_effect" and fault == "after_promotion_before_effect":
            hits["count"] += 1
            raise TransactionCrash(fault)

    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    kernel, key, resolved, validated, context, writer, workspace, project, store, _effects, _schemas = (
        make_effect_kernel(tmp_path, handler=handler, kind=kind, transaction_cut=cut)
    )
    if fault == "after_effect_apply_before_receipt":
        kernel.journal = _CrashAfterEffectJournal(kernel.journal, crash_on=EffectApplied)
    try:
        with pytest.raises(TransactionCrash):
            await kernel.execute_or_recover(key, resolved, validated, context)
        assert workspace.promotions == 1
        assert (project / "out.txt").read_bytes() == b"committed"

        if fault == "after_effect_apply_before_receipt":
            kernel.journal = kernel.journal.inner  # type: ignore[attr-defined]
            handler._apply_results = None
            handler._reconcile_results = EffectReconcileResult.applied({"remote_id": "r1"})

        replay = await kernel.execute_or_recover(
            key,
            resolved,
            validated,
            context,
            transaction_cut=None,
        )
        assert isinstance(replay, CommittedTaskResult)
        assert writer.calls == 1
        assert workspace.promotions == 1
        assert (project / "out.txt").read_bytes() == b"committed"
        expected_key = effect_idempotency_key(key, 1)
        if fault == "after_promotion_before_effect":
            assert handler.apply_keys == (expected_key,)
            assert handler.reconcile_keys == ()
        else:
            assert handler.apply_keys == (expected_key,)
            assert handler.reconcile_keys == (expected_key,)
        second = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(second, CommittedTaskResult)
        assert second.receipt == replay.receipt
        assert workspace.promotions == 1
        assert writer.calls == 1
    finally:
        store.close()


@pytest.mark.parametrize("kind", sorted(EXPECTED_EFFECT_KINDS))
async def test_crash_after_intent_applies_once_without_repeating_promotion(tmp_path: Path, kind: str) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    kernel, key, resolved, validated, context, writer, workspace, project, store, _effects, _schemas = (
        make_effect_kernel(tmp_path, handler=handler, kind=kind)
    )
    kernel.journal = _CrashAfterEffectJournal(kernel.journal, crash_on=EffectIntentRecorded)
    try:
        with pytest.raises(TransactionCrash, match="EffectIntentRecorded"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        assert workspace.promotions == 1
        kernel.journal = kernel.journal.inner  # type: ignore[attr-defined]
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, CommittedTaskResult)
        assert writer.calls == 1
        assert workspace.promotions == 1
        assert handler.apply_keys == (effect_idempotency_key(key, 1),)
        assert (project / "out.txt").read_bytes() == b"committed"
    finally:
        store.close()


@pytest.mark.parametrize("kind", sorted(EXPECTED_EFFECT_KINDS))
async def test_transient_then_reconcile_applied_does_not_repeat_promotion(tmp_path: Path, kind: str) -> None:
    handler = RecordingEffectHandler(
        apply_result=EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message="busy"),
        ),
        reconcile_result=EffectReconcileResult.applied({"remote_id": "r1"}),
    )
    policy = EffectPolicy(max_attempts=3, timeout_seconds=30, backoff_seconds=0)
    kernel, key, resolved, validated, context, writer, workspace, _project, store, _effects, _schemas = (
        make_effect_kernel(tmp_path, handler=handler, kind=kind, policy=policy)
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, IndeterminateTaskResult)
        assert workspace.promotions == 1
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, CommittedTaskResult)
        assert writer.calls == 1
        assert workspace.promotions == 1
        assert handler.apply_keys == (effect_idempotency_key(key, 1),)
        assert handler.reconcile_keys == (effect_idempotency_key(key, 1),)
    finally:
        store.close()
