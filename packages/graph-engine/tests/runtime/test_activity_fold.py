from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import StagedWriteSet, TaskOutcome, TaskWorkspaceIdentity
from graph_engine.runtime.checkpoint import write_checkpoint
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    InvocationStarted,
    NodeActivated,
    TaskActivityBound,
    TaskActivityCancelRequested,
    TaskActivityDispatchStarted,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskLeaseAdopted,
    TaskPromotionCompleted,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import FoldCursor, ProjectionError, fold_events

from bootstrap_fixtures import synthetic_invocation_started


_LOCK = "a" * 64
_PROMOTION = "c" * 64
_PROOF = "f" * 64


def _digest(value: object) -> str:
    return canonical_digest(value)  # type: ignore[arg-type]


def _identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(
        **payload,
        identity_digest=_digest(payload),
    )


def _staged(*, identity: TaskWorkspaceIdentity | None = None) -> StagedWriteSet:
    workspace_identity = identity or _identity()
    payload = {"identity_digest": workspace_identity.identity_digest, "files": []}
    return StagedWriteSet(
        identity_digest=workspace_identity.identity_digest,
        files=(),
        staged_digest=_digest(payload),
    )


def _fingerprint() -> dict[str, str]:
    return {"endpoint": "https://127.0.0.1:1", "executable": "runner"}


def _reference() -> dict[str, str]:
    return {"id": "ext-1"}


def _success_outcome() -> TaskOutcome:
    return TaskOutcome.succeeded({"answer": 42})


def _failed_outcome() -> TaskOutcome:
    return TaskOutcome.failed("transient", "provider closed")


def _stopped_outcome() -> TaskOutcome:
    return TaskOutcome.stopped("operator-stop")


def _envelopes(*events: object) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.from_event(seq, event)  # type: ignore[arg-type]
        for seq, event in enumerate(events, start=1)
    )


def _invocation_started() -> InvocationStarted:
    return synthetic_invocation_started(lock_digest=_LOCK)


def _running_prefix() -> tuple[object, ...]:
    return (
        _invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="task",
            payload=None,
        ),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
        NodeActivated(
            activation_id="a1",
            graph_instance_id="root",
            node_id="task",
            token_ids=("tok-1",),
        ),
        TaskAttemptStarted(activation_id="a1", attempt=1, lease_expires_at="11"),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
    )


def _prepared() -> TaskActivityPrepared:
    return TaskActivityPrepared(
        activity_id="activity-1",
        task_id="task-1",
        activation_id="a1",
        attempt=1,
        request_digest="0" * 64,
        workspace_identity=_identity(),
    )


def _dispatch() -> TaskActivityDispatchStarted:
    fingerprint = _fingerprint()
    return TaskActivityDispatchStarted(
        activity_id="activity-1",
        ordinal=1,
        dispatch_fingerprint=fingerprint,
        dispatch_fingerprint_digest=_digest(fingerprint),
    )


def _bound() -> TaskActivityBound:
    reference = _reference()
    return TaskActivityBound(
        activity_id="activity-1",
        reference=reference,
        reference_digest=_digest(reference),
    )


def _success_terminal() -> TaskActivityTerminalObserved:
    outcome = _success_outcome()
    return TaskActivityTerminalObserved(
        activity_id="activity-1",
        outcome=outcome,
        outcome_digest=_digest(outcome.model_dump(mode="json")),
        staged_write_set_digest=_staged().staged_digest,
        promotion_receipt_digest=None,
    )


def _failed_terminal(
    *, proof: str | None = None, promotion_receipt_digest: str | None = None
) -> TaskActivityTerminalObserved:
    outcome = _failed_outcome()
    return TaskActivityTerminalObserved(
        activity_id="activity-1",
        outcome=outcome,
        outcome_digest=_digest(outcome.model_dump(mode="json")),
        terminal_proof_digest=proof,
        staged_write_set_digest=_staged().staged_digest,
        promotion_receipt_digest=promotion_receipt_digest,
    )


def _success() -> TaskAttemptSucceeded:
    return TaskAttemptSucceeded(
        activation_id="a1",
        attempt=1,
        output={"answer": 42},
        staged_write_set_digest=_staged().staged_digest,
        promotion_receipt_digest=_PROMOTION,
    )


def _failed_attempt() -> TaskAttemptFailed:
    failure = _failed_outcome().failure
    assert failure is not None
    return TaskAttemptFailed(
        activation_id="a1",
        attempt=1,
        failure=failure,
        staged_write_set_digest=_staged().staged_digest,
    )


def _stopped_terminal(*, proof: str | None = None) -> TaskActivityTerminalObserved:
    outcome = _stopped_outcome()
    return TaskActivityTerminalObserved(
        activity_id="activity-1",
        outcome=outcome,
        outcome_digest=_digest(outcome.model_dump(mode="json")),
        terminal_proof_digest=proof,
        staged_write_set_digest=_staged().staged_digest,
    )


def _stopped_attempt() -> TaskAttemptStopped:
    return TaskAttemptStopped(
        activation_id="a1",
        attempt=1,
        reason="commit rejected",
        staged_write_set_digest=_staged().staged_digest,
    )


def _commit(*, staged: StagedWriteSet | None = None) -> TaskCommitPrepared:
    workspace_identity = _identity()
    write_set = staged or _staged(identity=workspace_identity)
    return TaskCommitPrepared(
        task_id="task-1",
        activation_id="a1",
        attempt=1,
        output={"answer": 42},
        workspace_identity=workspace_identity,
        staged_write_set=write_set,
        staged_write_set_digest=write_set.staged_digest,
    )


def _promotion(*, staged_write_set_digest: str | None = None) -> TaskPromotionCompleted:
    return TaskPromotionCompleted(
        task_id="task-1",
        activation_id="a1",
        attempt=1,
        staged_write_set_digest=staged_write_set_digest or _staged().staged_digest,
        promotion_receipt_digest=_PROMOTION,
    )


def _adopt() -> TaskLeaseAdopted:
    return TaskLeaseAdopted(
        activity_id="activity-1",
        task_id="task-1",
        activation_id="a1",
        attempt=1,
        owner_id="worker-2",
        acquired_at=2.0,
        heartbeat_at=2.0,
        expires_at=12.0,
        reconciliation_evidence_digest="e" * 64,
    )


def _bound_running_history() -> tuple[EventEnvelope, ...]:
    return _envelopes(*_running_prefix(), _prepared(), _dispatch(), _bound())


def _complete_recoverable_history() -> tuple[EventEnvelope, ...]:
    return _envelopes(
        *_running_prefix(),
        _prepared(),
        _dispatch(),
        _bound(),
        _success_terminal(),
        _commit(),
        _promotion(),
        _success(),
    )


def _write_history(tmp_path: Path, history: tuple[EventEnvelope, ...]) -> Ledger:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(tuple(item.event for item in history), expected_next_seq=1)
    return ledger


def _mutated_history(mutation: str) -> tuple[EventEnvelope, ...]:
    if mutation == "success_without_bound":
        return _envelopes(*_running_prefix(), _prepared(), _dispatch(), _success_terminal())
    if mutation == "duplicate_dispatch":
        return _envelopes(*_running_prefix(), _prepared(), _dispatch(), _dispatch())
    if mutation == "changed_reference":
        other = {"id": "ext-2"}
        return _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            TaskActivityBound(
                activity_id="activity-1",
                reference=other,
                reference_digest=_digest(other),
            ),
        )
    if mutation == "failed_with_candidate":
        return _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            _failed_terminal(),
            _commit(),
        )
    if mutation == "adopt_without_evidence":
        return _envelopes(*_running_prefix(), _adopt())
    raise AssertionError(f"unknown mutation {mutation!r}")


def test_fold_projects_complete_recoverable_activity() -> None:
    projection = fold_events(_complete_recoverable_history())
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.state == "terminal_observed"
    assert attempt.activity.reference == _reference()
    assert attempt.activity.staged_write_set_digest == _staged().staged_digest
    assert attempt.activity.promotion_receipt_digest is None
    assert attempt.status == "succeeded"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == _PROMOTION


def test_fold_projects_bound_running_activity() -> None:
    projection = fold_events(_bound_running_history())
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state == "bound"
    assert attempt.activity.reference == _reference()


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("success_without_bound", "success requires a bound activity"),
        ("duplicate_dispatch", "dispatch transition is already durable"),
        ("changed_reference", "activity reference is immutable"),
        ("failed_with_candidate", "failed terminal activity cannot prepare promotion"),
        ("adopt_without_evidence", "lease adoption requires reconciliation evidence"),
    ],
)
def test_fold_rejects_illegal_activity_histories(mutation: str, message: str) -> None:
    envelopes = _mutated_history(mutation)
    with pytest.raises(ProjectionError, match=message):
        fold_events(envelopes)


def test_effect_free_history_without_activity_remains_legal() -> None:
    staged = _staged()
    envelopes = _envelopes(
        *_running_prefix(),
        TaskCommitPrepared(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            output={"ok": True},
            workspace_identity=_identity(),
            staged_write_set=staged,
            staged_write_set_digest=staged.staged_digest,
        ),
        TaskPromotionCompleted(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest=_PROMOTION,
        ),
        TaskAttemptSucceeded(
            activation_id="a1",
            attempt=1,
            output={"ok": True},
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest=_PROMOTION,
        ),
    )
    attempt = fold_events(envelopes).activations[-1].attempts[-1]
    assert attempt.activity is None
    assert attempt.status == "succeeded"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.promotion_receipt_digest == _PROMOTION


def test_fold_rejects_recoverable_outcome_before_terminal() -> None:
    with pytest.raises(ProjectionError, match="terminal activity"):
        fold_events(_envelopes(*_running_prefix(), _prepared(), _dispatch(), _bound(), _success()))


def test_fold_rejects_promotion_that_does_not_match_staged_write_set() -> None:
    with pytest.raises(ProjectionError, match="promotion receipt"):
        fold_events(
            _envelopes(
                *_running_prefix(),
                _prepared(),
                _dispatch(),
                _bound(),
                _success_terminal(),
                _commit(),
                _promotion(staged_write_set_digest="e" * 64),
            )
        )


def test_fold_allows_prepared_failed_terminal_bypass() -> None:
    projection = fold_events(
        _envelopes(*_running_prefix(), _prepared(), _failed_terminal(), _failed_attempt())
    )
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.status == "failed"
    assert attempt.activity is not None
    assert attempt.activity.state == "terminal_observed"
    assert attempt.activity.dispatch_fingerprint is None
    assert attempt.prepared_commit is None


def test_fold_allows_unbound_failed_terminal_with_proof() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _failed_terminal(proof=_PROOF),
            _failed_attempt(),
        )
    )
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.status == "failed"
    assert attempt.activity is not None
    assert attempt.activity.terminal_proof_digest == _PROOF
    assert attempt.activity.reference is None


def test_fold_rejects_unbound_post_dispatch_terminal_without_proof() -> None:
    with pytest.raises(ProjectionError, match="terminal_proof_digest"):
        fold_events(_envelopes(*_running_prefix(), _prepared(), _dispatch(), _failed_terminal()))


def test_fold_keeps_cancel_overlay_through_terminal() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            TaskActivityCancelRequested(
                activity_id="activity-1",
                reason="timeout",
                requested_at=1.5,
            ),
            _success_terminal(),
            _commit(),
            _promotion(),
            _success(),
        )
    )
    activity = projection.activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.cancel_requested is True
    assert activity.cancel_reason == "timeout"
    assert activity.state == "terminal_observed"


def test_fold_adopts_lease_on_bound_activity() -> None:
    projection = fold_events(_envelopes(*_running_prefix(), _prepared(), _dispatch(), _bound(), _adopt()))
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.state == "bound"
    assert attempt.lease_owner_id == "worker-2"
    assert attempt.lease_heartbeat_at == 2.0
    assert attempt.lease_expires_at_value == 12.0


def test_fold_allows_retry_after_failed_terminal_activity() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            _failed_terminal(),
            _failed_attempt(),
            TaskAttemptStarted(activation_id="a1", attempt=2, lease_expires_at="12"),
            TaskLeaseAcquired(
                task_id="task-1",
                activation_id="a1",
                attempt=2,
                owner_id="worker-2",
                acquired_at=2.0,
                heartbeat_at=2.0,
                expires_at=12.0,
            ),
        )
    )
    attempts = projection.activations[-1].attempts
    assert attempts[0].status == "failed"
    assert attempts[0].activity is not None
    assert attempts[0].activity.state == "terminal_observed"
    assert attempts[1].attempt == 2
    assert attempts[1].activity is None
    assert attempts[1].status == "running"


def test_fold_allows_attempt_failure_after_succeeded_terminal() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            _success_terminal(),
            _failed_attempt(),
        )
    )
    attempt = projection.activations[-1].attempts[-1]
    activity = attempt.activity
    assert attempt.status == "failed"
    assert attempt.failure == _failed_outcome().failure
    assert activity is not None
    assert activity.state == "terminal_observed"
    assert activity.terminal is not None
    assert activity.terminal.status == "succeeded"
    assert activity.staged_write_set_digest == _staged().staged_digest
    assert activity.promotion_receipt_digest is None
    assert attempt.prepared_commit is None


def test_fold_allows_attempt_stop_after_succeeded_terminal() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            _success_terminal(),
            _stopped_attempt(),
        )
    )
    attempt = projection.activations[-1].attempts[-1]
    activity = attempt.activity
    assert attempt.status == "stopped"
    assert attempt.stop_reason == "commit rejected"
    assert activity is not None
    assert activity.terminal is not None
    assert activity.terminal.status == "succeeded"
    assert activity.staged_write_set_digest == _staged().staged_digest


@pytest.mark.parametrize("terminal", ["failed", "stopped"])
def test_fold_rejects_commit_after_non_success_terminal(terminal: str) -> None:
    observed = _failed_terminal() if terminal == "failed" else _stopped_terminal()
    with pytest.raises(ProjectionError, match="failed terminal activity cannot prepare promotion"):
        fold_events(_envelopes(*_running_prefix(), _prepared(), _dispatch(), _bound(), observed, _commit()))


@pytest.mark.parametrize(
    "prefix",
    [
        (_prepared(),),
        (_prepared(), _dispatch()),
        (_prepared(), _dispatch(), _bound()),
    ],
)
def test_fold_rejects_commit_while_recoverable_activity_is_live(prefix: tuple[object, ...]) -> None:
    with pytest.raises(ProjectionError, match="commit prepared before a succeeded terminal activity"):
        fold_events(_envelopes(*_running_prefix(), *prefix, _commit()))


def test_fold_allows_commit_after_succeeded_terminal() -> None:
    projection = fold_events(
        _envelopes(
            *_running_prefix(),
            _prepared(),
            _dispatch(),
            _bound(),
            _success_terminal(),
            _commit(),
        )
    )
    attempt = projection.activations[-1].attempts[-1]
    activity = attempt.activity
    assert attempt.status == "promotion_pending"
    assert attempt.prepared_commit is not None
    assert attempt.prepared_commit.staged_write_set_digest == _staged().staged_digest
    assert activity is not None
    assert activity.terminal is not None
    assert activity.terminal.status == "succeeded"
    assert activity.staged_write_set_digest == _staged().staged_digest


def test_partitioned_fold_matches_one_shot_activity_fold() -> None:
    history = _complete_recoverable_history()
    first = FoldCursor.initial().advance(history[:7])
    assert first.advance(history[7:]).projection == fold_events(history)
    assert fold_events(history).activations[-1].attempts[-1].activity is not None


def test_checkpoint_deletion_does_not_change_activity_projection(tmp_path: Path) -> None:
    history = _bound_running_history()
    ledger = _write_history(tmp_path, history)
    expected = fold_events(ledger.read_all())
    checkpoint = tmp_path / "checkpoint.json"
    write_checkpoint(checkpoint, expected, last_seq=len(history), ledger_envelopes=ledger.read_all())
    assert expected.activations[-1].attempts[-1].activity is not None
    checkpoint.unlink(missing_ok=True)
    assert fold_events(ledger.read_all()) == expected
