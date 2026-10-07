from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from graph_engine.attempts.events import (
    ActivityBound,
    ActivityDispatchStarted,
    ActivityPrepared,
    ActivityTerminalObserved,
    AttemptOpened,
    AttemptTerminated,
    CommitPrepared,
    ResourcesAuthorized,
    ResourcesReleased,
    SystemInterruptCompletionCheckpointed,
    SystemInterruptIssuanceAnchored,
    SystemInterruptIssued,
    WorkspacePromoted,
    fold_attempt_events,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import (
    ATTEMPT_JOURNAL_SCHEMA_VERSION,
    AttemptJournalIntegrityError,
    AttemptJournalPort,
    AttemptJournalRecord,
    MemoryAttemptJournal,
    decode_attempt_event,
    decode_attempt_journal_record,
)
from graph_engine.persistence.runner_lease import StaleFencingToken


def _attempt_key(label: str = "attempt") -> AttemptKey:
    return AttemptKey(digest=canonical_digest({"attempt": label}))


def _digest(label: str) -> str:
    return canonical_digest({"label": label})


def _opened(*, revision: int = 0) -> AttemptOpened:
    return AttemptOpened(
        contract_digest=_digest("contract"),
        input_digest=_digest("input"),
        graph_revision=_digest("revision"),
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
    )


def test_journal_port_exposes_attempt_persistence_and_generation_without_workflow_api() -> None:
    methods = {
        name
        for name, value in inspect.getmembers(AttemptJournalPort)
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"load", "append", "ensure_durable", "latest_generation", "register_generation"}
    assert "start_invocation" not in methods
    assert "transition" not in methods
    assert "advance_workflow" not in methods


def test_closed_event_set_covers_required_attempt_lifecycle() -> None:
    kinds = {
        AttemptOpened.kind,
        ResourcesAuthorized.kind,
        ActivityPrepared.kind,
        ActivityDispatchStarted.kind,
        ActivityBound.kind,
        ActivityTerminalObserved.kind,
        CommitPrepared.kind,
        WorkspacePromoted.kind,
        SystemInterruptIssued.kind,
        SystemInterruptIssuanceAnchored.kind,
        SystemInterruptCompletionCheckpointed.kind,
        AttemptTerminated.kind,
        ResourcesReleased.kind,
    }
    assert kinds == {
        "attempt_opened",
        "resources_authorized",
        "activity_prepared",
        "activity_dispatch_started",
        "activity_bound",
        "activity_terminal_observed",
        "commit_prepared",
        "workspace_promoted",
        "system_interrupt_issued",
        "system_interrupt_issuance_anchored",
        "system_interrupt_completion_checkpointed",
        "attempt_terminated",
        "resources_released",
    }


def test_system_interrupt_issued_includes_generation_ordinal_and_envelope_digest() -> None:
    issued = SystemInterruptIssued(
        generation=2,
        ordinal=1,
        envelope_digest=_digest("envelope"),
    )
    assert issued.generation == 2
    assert issued.ordinal == 1
    assert issued.envelope_digest == _digest("envelope")
    projection = issued.canonical_projection()
    assert projection["generation"] == 2
    assert projection["ordinal"] == 1
    assert projection["envelope_digest"] == _digest("envelope")


def test_issuance_anchor_does_not_retire_active_generation() -> None:
    events = (
        _opened(),
        SystemInterruptIssued(generation=1, ordinal=0, envelope_digest=_digest("env")),
        SystemInterruptIssuanceAnchored(
            generation=1,
            ordinal=0,
            envelope_digest=_digest("env"),
            checkpoint_id="cp-interrupt",
        ),
    )
    snapshot = fold_attempt_events(_attempt_key(), events)
    assert snapshot.active_interrupt is not None
    assert snapshot.active_interrupt.generation == 1
    assert snapshot.active_interrupt.ordinal == 0
    assert snapshot.active_interrupt.envelope_digest == _digest("env")
    assert snapshot.active_interrupt.issuance_anchored is True
    assert snapshot.active_interrupt.retired is False


def test_only_completion_checkpoint_retires_active_generation() -> None:
    events = (
        _opened(),
        SystemInterruptIssued(generation=1, ordinal=0, envelope_digest=_digest("env")),
        SystemInterruptIssuanceAnchored(
            generation=1,
            ordinal=0,
            envelope_digest=_digest("env"),
            checkpoint_id="cp-interrupt",
        ),
        SystemInterruptCompletionCheckpointed(
            generation=1,
            ordinal=0,
            envelope_digest=_digest("env"),
            checkpoint_id="cp-resumed",
        ),
    )
    snapshot = fold_attempt_events(_attempt_key(), events)
    assert snapshot.active_interrupt is None or snapshot.active_interrupt.retired is True
    assert snapshot.retired_generations == (1,)


async def test_identical_append_replay_is_idempotent() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    batch = (_opened(),)
    first = await journal.append(key, batch, expected_revision=0, fencing_token=4)
    second = await journal.append(key, batch, expected_revision=0, fencing_token=4)
    loaded = await journal.load(key)
    assert first == second
    assert loaded == first
    assert loaded is not None
    assert loaded.revision == 1
    assert loaded.fencing_token == 4


async def test_different_batch_at_same_expected_revision_conflicts() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    first_batch = (_opened(),)
    await journal.append(key, first_batch, expected_revision=0, fencing_token=4)
    with pytest.raises(AttemptJournalIntegrityError, match="compare-and-swap"):
        await journal.append(
            key,
            (
                AttemptOpened(
                    contract_digest=_digest("other-contract"),
                    input_digest=_digest("input"),
                    graph_revision=_digest("revision"),
                    invocation_id="inv-1",
                    public_entrypoint="execute",
                    semantic_node_id="execution.run",
                ),
            ),
            expected_revision=0,
            fencing_token=4,
        )
    loaded = await journal.load(key)
    assert loaded is not None
    assert loaded.contract_digest == _digest("contract")


async def test_revision_gap_is_rejected() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    with pytest.raises(AttemptJournalIntegrityError, match="gap"):
        await journal.append(key, (_opened(),), expected_revision=2, fencing_token=4)


async def test_corrupt_record_digest_is_rejected() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    record = AttemptJournalRecord.build(
        revision=0,
        attempt_key=key,
        fencing_token=4,
        events=(_opened(),),
    )
    drifted = replace(record, record_digest=_digest("tampered"))
    with pytest.raises(AttemptJournalIntegrityError, match="digest"):
        await journal.append_record(drifted, expected_revision=0, fencing_token=4)


async def test_stale_fence_cannot_append_after_newer_owner() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    await journal.append(key, (_opened(),), expected_revision=0, fencing_token=4)
    await journal.append(
        key,
        (ResourcesAuthorized(authorization_id=_digest("auth")),),
        expected_revision=1,
        fencing_token=5,
    )
    with pytest.raises(StaleFencingToken):
        await journal.append(
            key,
            (ResourcesReleased(authorization_id=_digest("auth")),),
            expected_revision=2,
            fencing_token=4,
        )


async def test_ensure_durable_establishes_visibility_barrier() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    await journal.append(key, (_opened(),), expected_revision=0, fencing_token=4)
    await journal.ensure_durable(key)
    loaded = await journal.load(key)
    assert loaded is not None
    assert journal.durable_revision(key) == loaded.revision


def test_activity_bound_folds_minimum_recovery_reference() -> None:
    reference = {
        "adapter": "opencode",
        "provider": "opencode",
        "model": "fixture-model",
        "session_id": "ses_raw_1",
        "message_id": "msg_raw_1",
        "prompt_digest": _digest("prompt"),
        "result_digest": _digest("result"),
    }
    snapshot = fold_attempt_events(
        _attempt_key(),
        (
            _opened(),
            ActivityBound(
                activity_id="activity-1",
                reference=reference,
                reference_digest=_digest("reference"),
            ),
        ),
    )
    assert snapshot.activity_state == "bound"
    assert snapshot.activity_reference == reference
    assert snapshot.activity_reference_digest == _digest("reference")
    assert "secret" not in snapshot.activity_reference


def test_journal_record_digest_is_canonical_projection() -> None:
    key = _attempt_key()
    record = AttemptJournalRecord.build(
        revision=0,
        attempt_key=key,
        fencing_token=4,
        events=(_opened(),),
    )
    assert record.record_digest == record.canonical_digest()
    assert record.record_digest == canonical_digest(record.canonical_projection())
    drifted = AttemptJournalRecord.build(
        revision=0,
        attempt_key=key,
        fencing_token=5,
        events=(_opened(),),
    )
    assert drifted.record_digest != record.record_digest


def test_current_schema_version_is_explicit() -> None:
    assert ATTEMPT_JOURNAL_SCHEMA_VERSION == "1"


def test_decode_attempt_event_rejects_unknown_kind_and_fields() -> None:
    event = decode_attempt_event(_opened().canonical_projection())
    assert event == _opened()
    with pytest.raises(AttemptJournalIntegrityError, match="kind"):
        decode_attempt_event({"kind": "legacy_opened", "contract_digest": _digest("c")})
    with pytest.raises(AttemptJournalIntegrityError, match="field"):
        decode_attempt_event({**_opened().canonical_projection(), "legacy": True})
    omitted = dict(_opened().canonical_projection())
    del omitted["invocation_id"]
    with pytest.raises(AttemptJournalIntegrityError, match="omitted"):
        decode_attempt_event(omitted)


def test_decode_attempt_journal_record_rejects_unknown_schema() -> None:
    key = _attempt_key()
    record = AttemptJournalRecord.build(
        revision=0,
        attempt_key=key,
        fencing_token=4,
        events=(_opened(),),
    )
    decoded = decode_attempt_journal_record(
        record.canonical_projection(),
        schema_version=ATTEMPT_JOURNAL_SCHEMA_VERSION,
        record_digest=record.record_digest,
    )
    assert decoded == record
    with pytest.raises(AttemptJournalIntegrityError, match="schema"):
        decode_attempt_journal_record(
            record.canonical_projection(),
            schema_version="0",
            record_digest=record.record_digest,
        )
    with pytest.raises(AttemptJournalIntegrityError, match="digest"):
        decode_attempt_journal_record(
            record.canonical_projection(),
            schema_version=ATTEMPT_JOURNAL_SCHEMA_VERSION,
            record_digest=_digest("tampered"),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "effect_intent_recorded",
            "effect_ordinal": 1,
            "effect_kind": "example.delivery.v1",
            "intent_digest": "a" * 64,
            "payload": {},
        },
        {"kind": "effect_applied", "effect_ordinal": 1, "apply_digest": "a" * 64},
        {"kind": "effect_receipt_recorded", "effect_ordinal": 1, "receipt_digest": "a" * 64},
        {
            "kind": "attempt_terminated",
            "resolution_kind": "committed_effect_failure",
            "output": None,
            "receipt_id": "",
            "receipt_digest": "",
            "reason": "failed",
            "failure_kind": "",
            "message": "",
        },
    ],
)
def test_old_effect_history_is_rejected_instead_of_reinterpreted(payload: object) -> None:
    with pytest.raises(AttemptJournalIntegrityError):
        decode_attempt_event(payload)
