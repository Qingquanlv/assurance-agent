from dataclasses import replace
from pathlib import Path

import pytest

from graph_engine.attempts.checkpoint import (
    ActiveSystemInterrupt,
    AttemptCheckpoint,
    AttemptPhase,
    AttemptResult,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_checkpoint import (
    AttemptCheckpointIntegrityError,
    MemoryAttemptCheckpointStore,
    decode_attempt_checkpoint,
    encode_attempt_checkpoint,
)
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease, StaleFencingToken


def checkpoint(label: str = "first", **changes) -> AttemptCheckpoint:
    return AttemptCheckpoint(
        attempt_key=AttemptKey(digest=canonical_digest(label)),
        revision=0,
        fencing_token=1,
        phase=AttemptPhase.AUTHORIZE,
        contract_digest="b" * 64,
        input_digest="c" * 64,
        graph_revision="d" * 64,
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        **changes,
    )


@pytest.fixture
def lease(tmp_path: Path) -> LocalInvocationRunnerLease:
    return LocalInvocationRunnerLease(tmp_path)


@pytest.mark.asyncio
async def test_checkpoint_cas_is_atomic_and_copies_nested_payloads(lease):
    owned = await lease.acquire("inv-1", owner_id="worker")
    try:
        store = MemoryAttemptCheckpointStore(lease.assert_current)
        initial = await store.commit(checkpoint(), expected_revision=0, fencing_token=1)
        assert initial.revision == 1
        payload = {"session": ["external-1"]}
        candidate = replace(
            initial,
            phase=AttemptPhase.RECONCILE,
            authorization_id="e" * 64,
            activity_id="call-1",
            activity_state="dispatch_started",
            activity_dispatch_fingerprint=payload,
            activity_dispatch_fingerprint_digest=canonical_digest(payload),
        )
        saved = await store.commit(candidate, expected_revision=1, fencing_token=1)
        payload["session"].append("mutated")
        saved.activity_dispatch_fingerprint["session"].append("also-mutated")
        loaded = await store.load(initial.attempt_key)
        assert loaded.phase is AttemptPhase.RECONCILE
        assert loaded.activity_dispatch_fingerprint == {"session": ["external-1"]}
        with pytest.raises(AttemptCheckpointIntegrityError, match="compare-and-swap"):
            await store.commit(replace(loaded, revision=1), expected_revision=1, fencing_token=1)
        assert (await store.load(initial.attempt_key)).revision == 2
    finally:
        await lease.release(owned)


@pytest.mark.asyncio
async def test_live_fence_rejects_old_worker_even_before_next_checkpoint_write(lease):
    old = await lease.acquire("inv-1", owner_id="old")
    store = MemoryAttemptCheckpointStore(lease.assert_current)
    saved = await store.commit(checkpoint(), expected_revision=0, fencing_token=1)
    await lease.release(old)
    new = await lease.acquire("inv-1", owner_id="new")
    try:
        with pytest.raises(StaleFencingToken):
            await store.commit(saved, expected_revision=1, fencing_token=1)
        advanced = await store.commit(replace(saved, fencing_token=2), expected_revision=1, fencing_token=2)
        assert advanced.fencing_token == 2
    finally:
        await lease.release(new)


@pytest.mark.asyncio
async def test_complete_proofs_cannot_be_erased_and_other_attempt_failure_is_retained(lease):
    owned = await lease.acquire("inv-1", owner_id="worker")
    try:
        store = MemoryAttemptCheckpointStore(lease.assert_current)
        failed = await store.commit(
            replace(
                checkpoint("failed"),
                phase=AttemptPhase.RELEASE,
                terminal=AttemptResult(
                    resolution_kind="retryable", failure_kind="timeout", message="lost call"
                ),
            ),
            expected_revision=0,
            fencing_token=1,
        )
        complete = replace(
            checkpoint(),
            phase=AttemptPhase.DONE,
            authorization_id="e" * 64,
            activity_id="call",
            activity_state="terminal_observed",
            activity_outcome={"ok": True},
            activity_outcome_digest=canonical_digest({"ok": True}),
            prepared_digest="f" * 64,
            promotion_receipt_id="receipt",
            promotion_receipt_digest="a" * 64,
            promotion_staged_digest="b" * 64,
            terminal=AttemptResult(output={"ok": True}, receipt_id="receipt", receipt_digest="a" * 64),
            released=True,
            active_interrupts=(
                ActiveSystemInterrupt(
                    generation=1,
                    ordinal=0,
                    envelope_digest="c" * 64,
                    issuance_checkpoint_id="anchor",
                    completion_checkpoint_id="complete",
                ),
            ),
            retired_generations=(1,),
        )
        saved = await store.commit(complete, expected_revision=0, fencing_token=1)
        for update in (
            {"terminal": None},
            {"prepared_digest": None},
            {"released": False},
            {"active_interrupts": ()},
            {"retired_generations": ()},
            {"input_digest": "e" * 64},
        ):
            with pytest.raises((ValueError, AttemptCheckpointIntegrityError)):
                await store.commit(replace(saved, **update), expected_revision=1, fencing_token=1)
        assert (await store.load(failed.attempt_key)).terminal.message == "lost call"
        assert len(await store.read_checkpoints()) == 2
        await store.ensure_durable(saved.attempt_key)
        assert (await store.load(saved.attempt_key)).revision == 1
    finally:
        await lease.release(owned)


@pytest.mark.parametrize(
    "update",
    [
        {"phase": "imaginary"},
        {"revision": True},
        {"fencing_token": False},
        {"phase": AttemptPhase.RECONCILE},
        {"phase": AttemptPhase.COMMIT},
        {"phase": AttemptPhase.DONE},
        {"activity_reference_digest": "e" * 64},
        {"source_identity_digest": "e" * 64},
    ],
)
def test_malformed_checkpoint_is_rejected(update):
    with pytest.raises(ValueError):
        replace(checkpoint(), **update)


def test_canonical_codec_rejects_unknown_fields_digest_drift_and_noncanonical_bytes():
    encoded = encode_attempt_checkpoint(checkpoint())
    assert (
        decode_attempt_checkpoint(
            encoded, record_digest=canonical_digest(checkpoint().canonical_projection())
        )
        == checkpoint()
    )
    with pytest.raises(AttemptCheckpointIntegrityError):
        decode_attempt_checkpoint(encoded, record_digest="0" * 64)
    with pytest.raises(AttemptCheckpointIntegrityError):
        decode_attempt_checkpoint(encoded + b" ")
    with pytest.raises(AttemptCheckpointIntegrityError):
        decode_attempt_checkpoint(encoded[:-1] + b',"unknown":true}')


@pytest.mark.asyncio
async def test_generation_budget_and_saved_input_are_defensively_copied(lease):
    store = MemoryAttemptCheckpointStore(lease.assert_current)
    scope = {"invocation_id": "inv-1"}
    data = {"task": ["first"]}
    first = await store.register_generation(
        scope, lambda n: AttemptKey(digest=canonical_digest(n)), max_attempts=1, validated_input=data
    )
    data["task"].append("mutated")
    assert (
        await store.register_generation(
            scope, lambda n: AttemptKey(digest=canonical_digest(n)), max_attempts=1
        )
        is None
    )
    assert await store.latest_generation(scope) == (1, first[1], False, {"task": ["first"]})
    await store.abandon_generations({first[1].digest})
    assert (await store.latest_generation(scope))[2] is True


@pytest.mark.asyncio
async def test_terminal_ordering_metadata_survives_later_owner_and_release(lease):
    store = MemoryAttemptCheckpointStore(lease.assert_current)
    old = await lease.acquire("inv-1", owner_id="old")
    saved = await store.commit(
        replace(
            checkpoint(),
            phase=AttemptPhase.RELEASE,
            terminal=AttemptResult(resolution_kind="retryable", failure_kind="timeout", message="failed"),
        ),
        expected_revision=0,
        fencing_token=1,
    )
    assert (saved.terminal_fencing_token, saved.terminal_revision) == (1, 1)
    await lease.release(old)
    new = await lease.acquire("inv-1", owner_id="new")
    try:
        candidate = replace(saved, fencing_token=2, released=True, phase=AttemptPhase.DONE)
        advanced = await store.commit(candidate, expected_revision=1, fencing_token=2)
        assert (advanced.fencing_token, advanced.terminal_fencing_token, advanced.terminal_revision) == (
            2,
            1,
            1,
        )
        with pytest.raises(AttemptCheckpointIntegrityError, match="terminal_fencing_token"):
            await store.commit(
                replace(advanced, terminal_fencing_token=2), expected_revision=2, fencing_token=2
            )
    finally:
        await lease.release(new)


@pytest.mark.asyncio
async def test_no_argument_memory_store_enforces_stored_fence_and_same_phase_progress():
    store = MemoryAttemptCheckpointStore()
    initial = await store.commit(checkpoint(), expected_revision=0, fencing_token=1)
    advanced = await store.commit(replace(initial, fencing_token=2), expected_revision=1, fencing_token=2)
    assert (advanced.phase, advanced.revision) == (AttemptPhase.AUTHORIZE, 2)
    with pytest.raises(StaleFencingToken):
        await store.commit(replace(advanced, fencing_token=1), expected_revision=2, fencing_token=1)


def test_pending_failure_cannot_enter_execute_and_bound_reference_needs_bound_state():
    authorized = replace(checkpoint(), phase=AttemptPhase.EXECUTE, authorization_id="e" * 64)
    with pytest.raises(ValueError, match="pending result"):
        replace(authorized, pending_result=AttemptResult(resolution_kind="retryable", message="failed"))
    with pytest.raises(ValueError, match="reference"):
        replace(
            authorized,
            activity_id="call",
            activity_state="prepared",
            activity_reference={"session": "external"},
            activity_reference_digest=canonical_digest({"session": "external"}),
        )


@pytest.mark.asyncio
async def test_commit_handler_can_save_preparation_and_promotion_without_changing_phase():
    store = MemoryAttemptCheckpointStore()
    observed = replace(
        checkpoint(),
        phase=AttemptPhase.COMMIT,
        authorization_id="e" * 64,
        activity_id="call",
        activity_state="terminal_observed",
        activity_outcome={"ok": True},
        activity_outcome_digest=canonical_digest({"ok": True}),
    )
    observed = await store.commit(observed, expected_revision=0, fencing_token=1)
    prepared = await store.commit(
        replace(observed, prepared_digest="f" * 64), expected_revision=1, fencing_token=1
    )
    with pytest.raises(AttemptCheckpointIntegrityError, match="prepared_digest"):
        await store.commit(replace(prepared, prepared_digest=None), expected_revision=2, fencing_token=1)
    promoted = await store.commit(
        replace(
            prepared,
            promotion_receipt_id="receipt",
            promotion_receipt_digest="a" * 64,
            promotion_staged_digest="b" * 64,
        ),
        expected_revision=2,
        fencing_token=1,
    )
    assert (promoted.phase, promoted.revision) == (AttemptPhase.COMMIT, 3)
    assert promoted.activity_outcome == {"ok": True}
