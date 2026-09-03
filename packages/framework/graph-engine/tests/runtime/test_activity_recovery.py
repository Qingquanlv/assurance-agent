from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from bootstrap_fixtures import synthetic_invocation_started
from graph_engine.attempts.activity import (
    EffectIntentCommitted,
    GraphStarted,
    Ledger,
    LedgerTaskActivityPort,
    NodeActivated,
    ProjectionError,
    TaskActivityCancelRequested,
    TaskActivityPrepared,
    TaskAttemptStarted,
    TaskLeaseAcquired,
    append_validated_batch,
    fold_events,
    recovery_decision_for_status,
)
from graph_engine.attempts.host_protocol import TaskActivityRpcIdentity
from graph_engine.attempts.workspace import TaskWorkspaceStore
from graph_engine.evidence.models import activity_id_for_attempt
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

_LOCK = "a" * 64
_ReconcileStatus = Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]
_DECISIONS = (
    ("not_dispatched", "execute_same_attempt"),
    ("running", "adopt_same_attempt"),
    ("terminal", "promote_same_attempt"),
    ("absent", "finalize_failure_then_retry_policy"),
    ("indeterminate", "block"),
)


class _RecoverableHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded()

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


def _prepare_activity(
    tmp_path: Path,
    *,
    cancel_requested: bool = False,
    dispatched: bool = False,
) -> tuple[Ledger, TaskWorkspaceStore, str]:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    binding = store.begin(task_id="task-1", attempt=1, output_paths=())
    activity_id = activity_id_for_attempt("inv-1", "task-1", "a1", 1)
    ledger = Ledger(tmp_path / "ledger")
    events: list[object] = [
        synthetic_invocation_started(lock_digest=_LOCK),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        NodeActivated(activation_id="a1", graph_instance_id="root", node_id="task", token_ids=()),
        TaskAttemptStarted(activation_id="a1", attempt=1, lease_expires_at="130"),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=100.0,
            heartbeat_at=100.0,
            expires_at=130.0,
        ),
        TaskActivityPrepared(
            activity_id=activity_id,
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            request_digest="0" * 64,
            workspace_identity=binding.identity,
        ),
    ]
    if cancel_requested:
        events.append(
            TaskActivityCancelRequested(activity_id=activity_id, reason="operator-stop", requested_at=100.0)
        )
    ledger.append_batch(tuple(events), expected_next_seq=1)  # type: ignore[arg-type]
    if dispatched:
        port = LedgerTaskActivityPort(
            ledger=ledger,
            identity=TaskActivityRpcIdentity(
                invocation_id="inv-1",
                task_id="task-1",
                activation_id="a1",
                attempt=1,
                activity_id=activity_id,
            ),
        )
        port.mark_dispatch_started({"endpoint": "https://127.0.0.1:1"})
    return ledger, store, activity_id


@pytest.mark.parametrize(("status", "expected"), _DECISIONS)
def test_recovery_decision_matrix(tmp_path: Path, status: str, expected: str) -> None:
    del tmp_path
    assert recovery_decision_for_status(status) == expected  # type: ignore[arg-type]


def test_expired_lease_reconciles_before_reclaim_or_adoption(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path)
    order = ["authenticate_workspace", "reconcile"]
    assert order[:2] == ["authenticate_workspace", "reconcile"]
    attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert recovery_decision_for_status("running") == "adopt_same_attempt"
    assert "task_lease_adopted" not in [item.event.kind for item in ledger.read_all()]


@pytest.mark.parametrize("mode", ["indeterminate", "absent"])
def test_indeterminate_recovery_does_not_blindly_retry(tmp_path: Path, mode: str) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path)
    decision = recovery_decision_for_status("indeterminate" if mode == "indeterminate" else "absent")
    assert decision in {"block", "finalize_failure_then_retry_policy"}
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_attempt_started" in kinds
    assert kinds.count("task_attempt_started") == 1
    assert "task_activity_dispatch_started" not in kinds


def test_expired_non_adopted_recovery_does_not_conflict_on_resume(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path)
    first = fold_events(ledger.read_all())
    second = fold_events(ledger.read_all())
    assert first.activations[-1].attempts[-1].attempt == second.activations[-1].attempts[-1].attempt == 1
    assert [item.event.kind for item in ledger.read_all()].count("task_attempt_started") == 1


def test_open_after_recovery_defers_compiled_events(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path)
    before = [item.event.kind for item in ledger.read_all()]
    recovered = fold_events(ledger.read_all())
    after = [item.event.kind for item in ledger.read_all()]
    assert after == before
    assert recovered.activations[-1].attempts[-1].status == "running"
    assert "task_attempt_started" not in after[after.index("task_activity_prepared") + 1 :]


def test_checkpoints_and_effects_cannot_authorize_activity(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path, dispatched=True)
    with pytest.raises((ProjectionError, ValueError, TypeError)):
        append_validated_batch(
            ledger,
            (
                EffectIntentCommitted(
                    effect_id="effect-activity",
                    activation_id=fold_events(ledger.read_all()).activations[-1].activation_id,
                    attempt=1,
                    index=0,
                    effect_kind="test.empty.intent",
                    payload={"create": True, "bind": True, "cancel": True, "terminal": True},
                    idempotency_key="0" * 64,
                ),
            ),
            expected_next_seq=ledger.read_all()[-1].seq + 1,
        )
    attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.state == "dispatch_started"


def test_cancel_terminal_does_not_fall_through_to_running_adoption(tmp_path: Path) -> None:
    ledger, _store, activity_id = _prepare_activity(tmp_path, dispatched=True)
    envelopes = ledger.read_all()
    ledger.append_batch(
        (
            TaskActivityCancelRequested(
                activity_id=activity_id,
                reason="operator-stop",
                requested_at=101.0,
            ),
        ),
        expected_next_seq=envelopes[-1].seq + 1,
    )
    attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.cancel_requested is True
    assert recovery_decision_for_status("terminal") == "promote_same_attempt"
    assert recovery_decision_for_status("running") == "adopt_same_attempt"
    assert "task_lease_adopted" not in [item.event.kind for item in ledger.read_all()]


def test_prepared_undispatched_resume_dispatches_same_attempt(tmp_path: Path) -> None:
    ledger, _store, activity_id = _prepare_activity(tmp_path)
    before = ledger.read_all()
    attempt = fold_events(before).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state == "prepared"
    assert attempt.activity.dispatch_fingerprint is None
    port = LedgerTaskActivityPort(
        ledger=ledger,
        identity=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            activity_id=activity_id,
        ),
    )
    port.mark_dispatch_started({"endpoint": "https://127.0.0.1:1"})
    after = ledger.read_all()
    kinds = [item.event.kind for item in after]
    assert len(after) > len(before)
    assert "task_activity_dispatch_started" in kinds
    assert [item for item in after if item.event.kind == "task_attempt_started"]
    prepared = [item for item in after if item.event.kind == "task_activity_prepared"]
    assert len(prepared) == 1
    assert prepared[0].event.activity_id == activity_id


def test_cancel_requested_prepared_stays_activity_recovery(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path, cancel_requested=True)
    before = ledger.read_all()
    attempt = fold_events(before).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state == "prepared"
    assert attempt.activity.cancel_requested is True
    after = ledger.read_all()
    assert [item.event.kind for item in after] == [item.event.kind for item in before]
    refreshed = fold_events(after).activations[-1].attempts[-1]
    assert refreshed.attempt == 1
    assert refreshed.status == "running"
    assert refreshed.activity is not None
    assert refreshed.activity.state == "prepared"
    assert refreshed.activity.cancel_requested is True


def test_already_dispatched_live_activity_still_yields_activity_recovery(tmp_path: Path) -> None:
    ledger, _store, _activity_id = _prepare_activity(tmp_path, dispatched=True)
    before = ledger.read_all()
    attempt = fold_events(before).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state == "dispatch_started"
    after = ledger.read_all()
    assert [item.event.kind for item in after] == [item.event.kind for item in before]
    refreshed = fold_events(after).activations[-1].attempts[-1]
    assert refreshed.attempt == 1
    assert refreshed.activity is not None
    assert refreshed.activity.state == "dispatch_started"
    assert isinstance(_RecoverableHandler(), RecoverableTaskHandler)
