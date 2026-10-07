from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from graph_engine.attempts import kernel as kernel_module
from graph_engine.attempts.contracts import ExecutedAttemptResult
from graph_engine.attempts.errors import AttemptIntegrityError
from graph_engine.attempts.events import AttemptSnapshot, AttemptTerminated
from graph_engine.attempts.handlers import AuthorizationActivityHandler
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.phase import AttemptPhaseIntegrityError
from graph_engine.attempts.resolutions import (
    AttemptResolution,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    RejectedTaskResult,
    SystemReference,
)
from graph_engine.attempts.runtime import (
    AttemptAction,
    AttemptRuntime,
    ContinueAttempt,
    ReturnResolution,
    RuntimeProgress,
    select_action,
)
from pydantic import RootModel

_HELPER_SPEC = importlib.util.spec_from_file_location(
    "runtime_kernel_helpers",
    Path(__file__).with_name("test_kernel.py"),
)
assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(_HELPERS)
kernel_fixture = _HELPERS.kernel_fixture


def snapshot(**updates: object) -> AttemptSnapshot:
    return replace(
        AttemptSnapshot(attempt_key=AttemptKey(digest="a" * 64), revision=1, fencing_token=1), **updates
    )


_SETUP = frozenset({AttemptAction.OPEN, AttemptAction.AUTHORIZE, AttemptAction.BEGIN_WORKSPACE})


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (snapshot(), AttemptAction.DISPATCH),
        (snapshot(contract_digest="b" * 64), AttemptAction.DISPATCH),
        (snapshot(authorization_id="c" * 64), AttemptAction.DISPATCH),
        (snapshot(activity_state="prepared"), AttemptAction.RECONCILE),
        (snapshot(activity_state="dispatch_started"), AttemptAction.RECONCILE),
        (snapshot(activity_state="bound"), AttemptAction.RECONCILE),
        (
            snapshot(activity_state="terminal_observed", activity_outcome={"value": "done"}),
            AttemptAction.ADOPT_RESULT,
        ),
        (
            snapshot(
                activity_state="terminal_observed",
                activity_outcome={"value": "done"},
                prepared_digest="d" * 64,
            ),
            AttemptAction.ADOPT_RESULT,
        ),
        (
            snapshot(
                activity_state="terminal_observed",
                activity_outcome={"value": "done"},
                prepared_digest="d" * 64,
                promotion_receipt_id="receipt",
                promotion_receipt_digest="e" * 64,
                promotion_staged_digest="f" * 64,
            ),
            AttemptAction.ADOPT_RESULT,
        ),
    ],
)
def test_routes_activity_using_durable_state(saved: AttemptSnapshot, expected: AttemptAction) -> None:
    assert select_action(saved, RuntimeProgress(completed=_SETUP)) is expected


@pytest.mark.parametrize("saved", [snapshot(), snapshot(prepared_digest="d" * 64)])
def test_call_local_setup_is_required_even_after_prepare(saved: AttemptSnapshot) -> None:
    assert select_action(None, RuntimeProgress()) is AttemptAction.OPEN
    assert (
        select_action(saved, RuntimeProgress(completed=frozenset({AttemptAction.OPEN})))
        is AttemptAction.AUTHORIZE
    )
    assert (
        select_action(saved, RuntimeProgress(completed=_SETUP - {AttemptAction.BEGIN_WORKSPACE}))
        is AttemptAction.BEGIN_WORKSPACE
    )


@pytest.mark.parametrize("released", [False, True])
def test_terminal_routes_to_replay_without_setup(released: bool) -> None:
    saved = snapshot(
        terminal=AttemptTerminated(resolution_kind="rejected", reason="closed"), released=released
    )
    assert (
        select_action(saved, RuntimeProgress(completed=frozenset({AttemptAction.OPEN})))
        is AttemptAction.REPLAY_TERMINAL
    )


def test_progress_does_not_require_a_phase_change() -> None:
    saved = snapshot(activity_state="bound")
    completed = _SETUP | {AttemptAction.RECONCILE}
    assert select_action(saved, RuntimeProgress(completed=completed)) is AttemptAction.COMMIT
    assert (
        select_action(saved, RuntimeProgress(completed=completed | {AttemptAction.COMMIT}))
        is AttemptAction.TERMINATE
    )
    assert select_action(saved, RuntimeProgress(completed=_SETUP, terminate=True)) is AttemptAction.TERMINATE


class RecordingHandlers:
    def __init__(
        self,
        saved: AttemptSnapshot,
        *,
        early: AttemptResolution | None = None,
        early_at: AttemptAction = AttemptAction.DISPATCH,
        crash: RuntimeError | None = None,
    ) -> None:
        self.saved = saved
        self.early = early
        self.early_at = early_at
        self.crash = crash
        self.actions: list[AttemptAction] = []
        self.result = RejectedTaskResult(reason="recorded")

    async def handle(self, action: AttemptAction, current: AttemptSnapshot | None):
        self.actions.append(action)
        if action is self.early_at and self.early is not None:
            return ReturnResolution(self.early)
        if action is AttemptAction.TERMINATE and self.crash is not None:
            raise self.crash
        if action in {AttemptAction.TERMINATE, AttemptAction.REPLAY_TERMINAL}:
            return ReturnResolution(self.result)
        # Deliberately keep the coarse phase unchanged through setup/activity.
        if action is AttemptAction.COMMIT:
            self.saved = replace(
                self.saved,
                promotion_receipt_id="receipt",
                promotion_receipt_digest="e" * 64,
                promotion_staged_digest="f" * 64,
            )
        return ContinueAttempt(self.saved)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "early",
    [
        PendingTaskResult(wakeup=SystemReference(reference_id="waiting")),
        IndeterminateTaskResult(reconciliation=SystemReference(reference_id="unknown")),
    ],
)
async def test_runtime_returns_waiting_results_immediately(early: AttemptResolution) -> None:
    handlers = RecordingHandlers(snapshot(), early=early)
    assert await AttemptRuntime(handlers).run() is early
    assert handlers.actions == [
        AttemptAction.OPEN,
        AttemptAction.AUTHORIZE,
        AttemptAction.BEGIN_WORKSPACE,
        AttemptAction.DISPATCH,
    ]


@pytest.mark.asyncio
async def test_runtime_returns_pending_authorization_without_workspace() -> None:
    pending = PendingTaskResult(wakeup=SystemReference(reference_id="blocked"))
    handlers = RecordingHandlers(snapshot(), early=pending, early_at=AttemptAction.AUTHORIZE)
    assert await AttemptRuntime(handlers).run() is pending
    assert handlers.actions == [AttemptAction.OPEN, AttemptAction.AUTHORIZE]


@pytest.mark.asyncio
async def test_runtime_runs_a_finite_ordered_protocol() -> None:
    handlers = RecordingHandlers(snapshot())
    assert await AttemptRuntime(handlers).run() is handlers.result
    assert handlers.actions == [
        AttemptAction.OPEN,
        AttemptAction.AUTHORIZE,
        AttemptAction.BEGIN_WORKSPACE,
        AttemptAction.DISPATCH,
        AttemptAction.COMMIT,
        AttemptAction.TERMINATE,
    ]


@pytest.mark.asyncio
async def test_terminal_replay_never_runs_activity() -> None:
    handlers = RecordingHandlers(
        snapshot(terminal=AttemptTerminated(resolution_kind="rejected", reason="closed"))
    )
    assert await AttemptRuntime(handlers).run() is handlers.result
    assert handlers.actions == [AttemptAction.OPEN, AttemptAction.REPLAY_TERMINAL]


@pytest.mark.asyncio
async def test_exception_after_promotion_propagates_unchanged() -> None:
    crash = RuntimeError("after promotion")
    handlers = RecordingHandlers(snapshot(), crash=crash)
    with pytest.raises(RuntimeError) as raised:
        await AttemptRuntime(handlers).run()
    assert raised.value is crash
    assert handlers.saved.promotion_receipt_id == "receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cut", ["after_observed_result", "after_prepare_before_promotion", "after_promotion_before_receipt"]
)
async def test_saved_recovery_snapshot_rebuilds_local_prerequisites(kernel_fixture, cut: str) -> None:
    await kernel_fixture.crash_after(cut)
    saved = await kernel_fixture.journal.load(kernel_fixture.attempt_key)
    assert saved is not None
    handlers = RecordingHandlers(saved)
    assert await AttemptRuntime(handlers).run() is handlers.result
    assert handlers.actions == [
        AttemptAction.OPEN,
        AttemptAction.AUTHORIZE,
        AttemptAction.BEGIN_WORKSPACE,
        AttemptAction.ADOPT_RESULT,
        AttemptAction.COMMIT,
        AttemptAction.TERMINATE,
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_a_terminal_handler_that_does_not_return() -> None:
    class NonReturningHandler(RecordingHandlers):
        async def handle(self, action: AttemptAction, current: AttemptSnapshot | None):
            if action is AttemptAction.TERMINATE:
                self.actions.append(action)
                return ContinueAttempt(self.saved)
            return await super().handle(action, current)

    handlers = NonReturningHandler(snapshot())
    with pytest.raises(RuntimeError, match="did not finish terminate"):
        await AttemptRuntime(handlers).run()
    assert handlers.actions.count(AttemptAction.TERMINATE) == 1


@pytest.mark.asyncio
async def test_failure_requests_terminal_publication_without_commit() -> None:
    class FailedActivityHandler(RecordingHandlers):
        async def handle(self, action: AttemptAction, current: AttemptSnapshot | None):
            if action is AttemptAction.DISPATCH:
                self.actions.append(action)
                return ContinueAttempt(self.saved, terminate=True)
            return await super().handle(action, current)

    handlers = FailedActivityHandler(snapshot())
    assert await AttemptRuntime(handlers).run() is handlers.result
    assert handlers.actions == [
        AttemptAction.OPEN,
        AttemptAction.AUTHORIZE,
        AttemptAction.BEGIN_WORKSPACE,
        AttemptAction.DISPATCH,
        AttemptAction.TERMINATE,
    ]


@pytest.mark.asyncio
async def test_reusable_facade_uses_replaced_public_port_and_new_local_progress(kernel_fixture) -> None:
    await kernel_fixture.crash_after("terminal_durable")

    class ReplacementArbiter:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.releases = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def release(self, attempt_key: AttemptKey, *, fencing_token: int) -> None:
            self.releases += 1
            await self.inner.release(attempt_key, fencing_token=fencing_token)

    replacement = ReplacementArbiter(kernel_fixture.kernel.arbiter)
    kernel_fixture.kernel.arbiter = replacement
    first = await kernel_fixture.restart()
    second = await kernel_fixture.restart()
    assert isinstance(first, CommittedTaskResult)
    assert second == first
    assert replacement.releases == 1
    assert kernel_fixture.executor.calls == 1
    assert (await kernel_fixture.snapshot()).released


@pytest.mark.parametrize(
    "saved",
    [
        snapshot(activity_state="terminal_observed", activity_outcome=None),
        snapshot(activity_state="terminal_observed", activity_outcome=None, prepared_digest="d" * 64),
        snapshot(
            activity_state="terminal_observed",
            activity_outcome=None,
            prepared_digest="d" * 64,
            promotion_receipt_id="receipt",
            promotion_receipt_digest="e" * 64,
            promotion_staged_digest="f" * 64,
        ),
    ],
)
def test_null_observed_output_routes_to_legacy_dispatch(saved: AttemptSnapshot) -> None:
    assert select_action(saved, RuntimeProgress(completed=_SETUP)) is AttemptAction.DISPATCH


@pytest.mark.asyncio
async def test_null_output_kernel_recovery_selects_its_actual_dispatch(tmp_path: Path, monkeypatch) -> None:
    executor = _HELPERS._ConfigurableExecutor()
    executor.result = ExecutedAttemptResult(output=RootModel[None](None))
    kernel, key, resolved, validated, context, _, _, store = _HELPERS.make_kernel(
        tmp_path,
        executor=executor,
        output_model=RootModel[None],
    )
    actions: list[AttemptAction] = []

    class RecordingPort:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def handle(self, action: AttemptAction, saved: AttemptSnapshot | None):
            actions.append(action)
            return await self.inner.handle(action, saved)

    class RecordingRuntime(AttemptRuntime):
        def __init__(self, handlers: Any) -> None:
            super().__init__(RecordingPort(handlers))

    monkeypatch.setattr(kernel_module, "AttemptRuntime", RecordingRuntime)

    def cut(name: str) -> None:
        if name == "after_observed_result":
            raise _HELPERS.TransactionCrash(name)

    try:
        with pytest.raises(_HELPERS.TransactionCrash):
            await kernel.execute_or_recover(key, resolved, validated, context, transaction_cut=cut)
        saved = await kernel.journal.load(key)
        assert saved is not None and saved.activity_state == "terminal_observed"
        assert saved.activity_outcome is None
        assert executor.calls == 1
        actions.clear()
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        assert result.output.root is None
        assert executor.calls == 2
        assert actions == [
            AttemptAction.OPEN,
            AttemptAction.AUTHORIZE,
            AttemptAction.BEGIN_WORKSPACE,
            AttemptAction.DISPATCH,
            AttemptAction.COMMIT,
            AttemptAction.TERMINATE,
        ]
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", [AttemptAction.ADOPT_RESULT, AttemptAction.RECONCILE, AttemptAction.COMMIT]
)
async def test_activity_handler_rejects_invalid_selected_branch(
    kernel_fixture, action: AttemptAction
) -> None:
    await kernel_fixture.crash_after("after_observed_result")
    saved = await kernel_fixture.journal.load(kernel_fixture.attempt_key)
    assert saved is not None
    saved = replace(saved, activity_outcome=None)
    handler = AuthorizationActivityHandler(
        kernel_fixture.kernel.journal,
        kernel_fixture.kernel.arbiter,
        kernel_fixture.kernel.workspace,
        kernel_fixture.kernel.graph_revision,
        None,
    )
    with pytest.raises(AttemptIntegrityError):
        await handler.activity(
            kernel_fixture.attempt_key,
            kernel_fixture.resolved,
            kernel_fixture.validated,
            kernel_fixture.executor.seen_scope,
            saved,
            lambda _: None,
            action,
        )
    assert kernel_fixture.executor.calls == 1


@pytest.mark.parametrize(
    "saved",
    [
        snapshot(activity_state="unknown"),
        snapshot(promotion_receipt_id="partial"),
        snapshot(released=True),
    ],
)
def test_selector_preserves_malformed_snapshot_integrity_checks(saved: AttemptSnapshot) -> None:
    with pytest.raises(AttemptPhaseIntegrityError):
        select_action(saved, RuntimeProgress(completed=_SETUP))
