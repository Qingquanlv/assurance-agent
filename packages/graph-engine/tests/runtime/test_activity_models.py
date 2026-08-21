from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskOutcome,
)
from graph_engine.runtime.activity import (
    AttemptWorkspaceLost,
    TaskActivityConflict,
    TaskActivityIndeterminate,
    TaskActivityProtocolViolation,
    TaskActivityRecoveryUnsupported,
    TaskActivityReferenceInvalid,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    TaskActivityBound,
    TaskActivityCancelRequested,
    TaskActivityDispatchStarted,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskLeaseAdopted,
)


GOLDEN = Path(__file__).with_name("activity-events-v2.golden.json")
_ACTIVITY_ERRORS = (
    TaskActivityConflict,
    TaskActivityIndeterminate,
    TaskActivityReferenceInvalid,
    TaskActivityRecoveryUnsupported,
    AttemptWorkspaceLost,
    TaskActivityProtocolViolation,
)


def _identity() -> AttemptWorkspaceIdentity:
    return AttemptWorkspaceIdentity(
        attempt_directory_id="attempt-1",
        baseline_tree_id="a" * 64,
        attempt_identity_digest="b" * 64,
    )


def _digest(value: object) -> str:
    return canonical_digest(value)  # type: ignore[arg-type]


def _outcome_digest(outcome: TaskOutcome) -> str:
    return _digest(outcome.model_dump(mode="json"))


def _fingerprint() -> dict[str, str]:
    return {"endpoint": "https://127.0.0.1:1", "executable": "runner"}


def _reference() -> dict[str, str]:
    return {"id": "ext-1"}


def _all_six_activity_events() -> tuple[
    TaskActivityPrepared,
    TaskActivityDispatchStarted,
    TaskActivityBound,
    TaskActivityCancelRequested,
    TaskActivityTerminalObserved,
    TaskLeaseAdopted,
]:
    fingerprint = _fingerprint()
    reference = _reference()
    outcome = TaskOutcome.succeeded({"answer": 42})
    return (
        TaskActivityPrepared(
            activity_id="activity-1",
            task_id="task-1",
            activation_id="act-1",
            attempt=1,
            request_digest="0" * 64,
            workspace_identity=_identity(),
        ),
        TaskActivityDispatchStarted(
            activity_id="activity-1",
            ordinal=1,
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
        ),
        TaskActivityBound(
            activity_id="activity-1",
            reference=reference,
            reference_digest=_digest(reference),
        ),
        TaskActivityCancelRequested(
            activity_id="activity-1",
            reason="timeout",
            requested_at=1.0,
        ),
        TaskActivityTerminalObserved(
            activity_id="activity-1",
            outcome=outcome,
            outcome_digest=_outcome_digest(outcome),
            candidate_tree_id="c" * 64,
            write_set_digest="d" * 64,
        ),
        TaskLeaseAdopted(
            activity_id="activity-1",
            task_id="task-1",
            activation_id="act-1",
            attempt=1,
            owner_id="worker-2",
            acquired_at=2.0,
            heartbeat_at=2.0,
            expires_at=12.0,
            reconciliation_evidence_digest="e" * 64,
        ),
    )


def test_terminal_activity_requires_exact_success_candidate() -> None:
    outcome = TaskOutcome.succeeded({"answer": 42})
    with pytest.raises(ValueError, match="candidate tree and write-set"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="terminal_observed",
            terminal=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
        )


def test_activity_event_golden_is_canonical() -> None:
    events = _all_six_activity_events()
    assert canonical_json_bytes([event.model_dump(mode="json") for event in events]) == (
        GOLDEN.read_bytes()
    )


def test_prepared_activity_forbids_a_bound_reference() -> None:
    reference = _reference()
    with pytest.raises(ValueError, match="reference"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="prepared",
            reference=reference,
            reference_digest=_digest(reference),
        )


def test_dispatch_started_activity_forbids_a_bound_reference() -> None:
    fingerprint = _fingerprint()
    reference = _reference()
    with pytest.raises(ValueError, match="reference"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="dispatch_started",
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
            reference=reference,
            reference_digest=_digest(reference),
        )


def test_bound_activity_requires_a_reference() -> None:
    fingerprint = _fingerprint()
    with pytest.raises(ValueError, match="reference"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="bound",
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
        )


def test_terminal_activity_requires_canonical_outcome_digest() -> None:
    fingerprint = _fingerprint()
    reference = _reference()
    outcome = TaskOutcome.succeeded({"answer": 42})
    with pytest.raises(ValueError, match="outcome digest"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="terminal_observed",
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
            reference=reference,
            reference_digest=_digest(reference),
            terminal=outcome,
            outcome_digest="0" * 64,
            candidate_tree_id="c" * 64,
            write_set_digest="d" * 64,
        )


def test_failed_terminal_activity_forbids_a_candidate() -> None:
    fingerprint = _fingerprint()
    outcome = TaskOutcome.failed("timeout", "lost")
    with pytest.raises(ValueError, match="candidate tree and write-set"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="terminal_observed",
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
            terminal=outcome,
            outcome_digest=_outcome_digest(outcome),
            terminal_proof_digest="f" * 64,
            candidate_tree_id="c" * 64,
            write_set_digest="d" * 64,
        )


def test_unbound_terminal_after_dispatch_requires_proof_digest() -> None:
    fingerprint = _fingerprint()
    outcome = TaskOutcome.failed("timeout", "lost")
    with pytest.raises(ValueError, match="terminal_proof_digest"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="terminal_observed",
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=_digest(fingerprint),
            terminal=outcome,
            outcome_digest=_outcome_digest(outcome),
        )


def test_succeeded_terminal_activity_accepts_exact_candidate() -> None:
    fingerprint = _fingerprint()
    reference = _reference()
    outcome = TaskOutcome.succeeded({"answer": 42})
    snapshot = TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="0" * 64,
        workspace_identity=_identity(),
        state="terminal_observed",
        dispatch_fingerprint=fingerprint,
        dispatch_fingerprint_digest=_digest(fingerprint),
        reference=reference,
        reference_digest=_digest(reference),
        terminal=outcome,
        outcome_digest=_outcome_digest(outcome),
        candidate_tree_id="c" * 64,
        write_set_digest="d" * 64,
    )
    assert snapshot.candidate_tree_id == "c" * 64
    assert snapshot.write_set_digest == "d" * 64


def test_reconcile_requires_outcome_exactly_for_terminal() -> None:
    with pytest.raises(ValueError, match="outcome"):
        TaskActivityReconcileResult(status="terminal")
    with pytest.raises(ValueError, match="outcome"):
        TaskActivityReconcileResult(status="running", outcome=TaskOutcome.succeeded({"ok": True}))
    result = TaskActivityReconcileResult(status="terminal", outcome=TaskOutcome.succeeded({"ok": True}))
    assert result.outcome is not None


def test_reconcile_requires_canonical_proof_exactly_for_absent() -> None:
    with pytest.raises(ValueError, match="proof"):
        TaskActivityReconcileResult(status="absent")
    with pytest.raises(ValueError, match="proof"):
        TaskActivityReconcileResult(status="not_dispatched", proof={"none": True})
    result = TaskActivityReconcileResult(status="absent", proof={"none": True})
    assert result.proof == {"none": True}


def test_reconcile_forbids_references_on_states_that_do_not_bind_one() -> None:
    reference = _reference()
    with pytest.raises(ValueError, match="reference"):
        TaskActivityReconcileResult(status="not_dispatched", reference=reference)
    with pytest.raises(ValueError, match="reference"):
        TaskActivityReconcileResult(status="absent", proof={"none": True}, reference=reference)


def test_reconcile_requires_non_empty_reason_for_indeterminate() -> None:
    with pytest.raises(ValueError, match="reason"):
        TaskActivityReconcileResult(status="indeterminate")
    with pytest.raises(ValueError, match="reason"):
        TaskActivityReconcileResult(status="indeterminate", reason="")
    result = TaskActivityReconcileResult(status="indeterminate", reason="ambiguous")
    assert result.reason == "ambiguous"


def test_cancel_requires_outcome_exactly_for_terminal() -> None:
    with pytest.raises(ValueError, match="outcome"):
        TaskActivityCancelResult(status="terminal")
    with pytest.raises(ValueError, match="outcome"):
        TaskActivityCancelResult(status="acknowledged", outcome=TaskOutcome.stopped("cancelled"))
    result = TaskActivityCancelResult(status="terminal", outcome=TaskOutcome.stopped("cancelled"))
    assert result.outcome is not None


def test_cancel_requires_non_empty_reason_for_indeterminate() -> None:
    with pytest.raises(ValueError, match="reason"):
        TaskActivityCancelResult(status="indeterminate")
    result = TaskActivityCancelResult(status="indeterminate", reason="still running")
    assert result.reason == "still running"


def test_workspace_identity_rejects_a_host_path() -> None:
    with pytest.raises(ValueError, match="host path"):
        AttemptWorkspaceIdentity(
            attempt_directory_id="/tmp/attempt-1",
            baseline_tree_id="a" * 64,
            attempt_identity_digest="b" * 64,
        )


def test_activity_values_do_not_import_runtime_modules() -> None:
    source = Path(__file__).parents[2] / "graph_engine" / "plugin_api.py"
    assert "graph_engine.runtime" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize("error_type", _ACTIVITY_ERRORS)
def test_activity_errors_are_graph_engine_errors(error_type: type[GraphEngineError]) -> None:
    assert issubclass(error_type, GraphEngineError)
    with pytest.raises(error_type, match="closed"):
        raise error_type("closed")


def test_activity_events_round_trip_through_strict_envelopes() -> None:
    events = _all_six_activity_events()
    restored = tuple(
        EventEnvelope.model_validate_json(
            EventEnvelope.from_event(seq, event).model_dump_json(),
            strict=True,
        ).event
        for seq, event in enumerate(events, start=1)
    )
    assert restored == events
    assert tuple(event.kind for event in restored) == (
        "task_activity_prepared",
        "task_activity_dispatch_started",
        "task_activity_bound",
        "task_activity_cancel_requested",
        "task_activity_terminal_observed",
        "task_lease_adopted",
    )
