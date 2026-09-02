from bootstrap_fixtures import synthetic_invocation_started
import json
import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import graph_engine.runtime.ledger as ledger_runtime
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import StagedWriteSet, TaskFailure, TaskOutcome, TaskWorkspaceIdentity
from graph_engine.runtime.checkpoint import load_checkpoint, write_checkpoint
from graph_engine.runtime.events import (
    EffectApplyStarted,
    EffectIntentCommitted,
    EffectReceiptRecorded,
    EventEnvelope,
    GraphCompleted,
    GraphFailed,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    NodeInterrupted,
    TaskActivityBound,
    TaskActivityCancelRequested,
    TaskActivityDispatchStarted,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptCommittedEffectFailed,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskLeaseAdopted,
    TaskLeaseHeartbeat,
    TaskPromotionCompleted,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import MAX_SEQUENCE, Ledger, LedgerConflictError, LedgerIntegrityError
from graph_engine.runtime.models import (
    FoldCursor,
    GraphInstanceRecord,
    InvocationProjection,
    PendingInterrupt,
    ProjectionError,
    TokenRecord,
    fold_events,
)


def _interrupt_event() -> NodeInterrupted:
    return NodeInterrupted(
        activation_id="act-1",
        interrupt_id="int-1",
        graph_instance_id="root",
        reason="review",
        actions=("continue",),
        input=None,
    )


def test_atomic_batches_have_contiguous_sequences(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(),
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="start",
                payload=None,
            ),
        ),
        expected_next_seq=1,
    )
    events = ledger.read_all()
    assert [item.seq for item in events] == [1, 2]
    assert fold_events(events).status == "running"


def test_temporary_batch_is_never_visible(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    (ledger.root / ".pending-1.json").write_text("{broken", encoding="utf-8")
    assert ledger.read_all() == ()


def test_sequence_gap_is_integrity_failure(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    (ledger.root / "0000000002-0000000002.json").write_text("[]", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="expected batch starting at 1"):
        ledger.read_all()


def test_append_requires_nonempty_batch_and_expected_sequence(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    with pytest.raises(ValueError, match="must not be empty"):
        ledger.append_batch((), expected_next_seq=1)
    with pytest.raises(ValueError, match="positive"):
        ledger.append_batch(
            (synthetic_invocation_started(),),
            expected_next_seq=0,
        )


def test_append_rejects_stale_writer_without_changing_final_batch(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (synthetic_invocation_started(),),
        expected_next_seq=1,
    )
    final = ledger.root / "0000000001-0000000001.json"
    original = final.read_bytes()

    with pytest.raises(LedgerConflictError, match="expected next sequence 1, found 2"):
        ledger.append_batch(
            (
                TokenOffered(
                    token_id="tok-1",
                    graph_instance_id="root",
                    source=None,
                    target="start",
                    payload=None,
                ),
            ),
            expected_next_seq=1,
        )

    assert final.read_bytes() == original


def test_validated_append_rejects_invalid_fold_before_publication(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (synthetic_invocation_started(),),
        expected_next_seq=1,
    )
    append = getattr(ledger_runtime, "append_validated_batch", None)
    assert callable(append), "runtime ledger must provide the centralized append protocol"

    with pytest.raises(ProjectionError):
        append(
            ledger,
            (synthetic_invocation_started(invocation_id="inv-2", lock_digest="b" * 64),),
            expected_next_seq=2,
        )
    assert len(ledger.read_all()) == 1


def test_validated_append_reports_indeterminate_reconciliation_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path / "ledger")
    append = getattr(ledger_runtime, "append_validated_batch", None)
    error_type = getattr(ledger_runtime, "LedgerPublicationIndeterminate", None)
    assert callable(append) and isinstance(error_type, type)
    original_append = ledger.append_batch
    original_read = ledger.read_all
    reads = 0

    def publish_then_raise(events, expected_next_seq):  # type: ignore[no-untyped-def]
        original_append(events, expected_next_seq)
        raise OSError("append result unavailable")

    def unreadable_reconciliation():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OSError("reconciliation unavailable")
        return original_read()

    monkeypatch.setattr(ledger, "append_batch", publish_then_raise)
    monkeypatch.setattr(ledger, "read_all", unreadable_reconciliation)
    with pytest.raises(error_type, match="indeterminate"):
        append(
            ledger,
            (synthetic_invocation_started(),),
            expected_next_seq=1,
        )


def test_validated_append_syncs_visible_exact_range_before_acknowledging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(tmp_path / "ledger")
    installed = False
    durable_after_install = False
    original_fsync = os.fsync

    def fail_after_link(name: str) -> None:
        nonlocal installed
        if name == "final_installed":
            installed = True
            raise OSError("link result unavailable")

    def track_fsync(descriptor: int) -> None:
        nonlocal durable_after_install
        if installed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            durable_after_install = True
        original_fsync(descriptor)

    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_link)
    monkeypatch.setattr(ledger_runtime.os, "fsync", track_fsync)

    ledger_runtime.append_validated_batch(
        ledger,
        (synthetic_invocation_started(),),
        expected_next_seq=1,
    )

    assert durable_after_install


def test_visible_exact_range_with_failed_durability_barrier_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(tmp_path / "ledger")
    installed = False
    original_fsync = os.fsync

    def fail_after_link(name: str) -> None:
        nonlocal installed
        if name == "final_installed":
            installed = True
            raise OSError("link result unavailable")

    def fail_reconciliation_barrier(descriptor: int) -> None:
        if installed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("ledger directory durability unavailable")
        original_fsync(descriptor)

    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_link)
    monkeypatch.setattr(ledger_runtime.os, "fsync", fail_reconciliation_barrier)

    with pytest.raises(ledger_runtime.LedgerPublicationIndeterminate, match="durab"):
        ledger_runtime.append_validated_batch(
            ledger,
            (synthetic_invocation_started(),),
            expected_next_seq=1,
        )


def test_append_never_replaces_an_invalid_colliding_final_file(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    final = ledger.root / "0000000001-0000000001.json"
    final.write_text("not-json", encoding="utf-8")
    original = final.read_bytes()

    with pytest.raises(LedgerIntegrityError, match="malformed JSON"):
        ledger.append_batch(
            (synthetic_invocation_started(),),
            expected_next_seq=1,
        )

    assert final.read_bytes() == original


@pytest.mark.parametrize(
    ("name", "contents", "match"),
    [
        ("batch.json", "[]", "invalid final batch filename"),
        ("0000000001-0000000001.json", "{broken", "malformed JSON"),
        ("0000000001-0000000002.json", "[]", "must contain at least one envelope"),
    ],
)
def test_malformed_final_files_are_rejected(tmp_path: Path, name: str, contents: str, match: str) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    (ledger.root / name).write_text(contents, encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match=match):
        ledger.read_all()


def test_final_filename_must_match_envelope_range(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (synthetic_invocation_started(),),
        expected_next_seq=1,
    )
    source = ledger.root / "0000000001-0000000001.json"
    source.rename(ledger.root / "0000000001-0000000002.json")
    with pytest.raises(LedgerIntegrityError, match="does not match envelope range"):
        ledger.read_all()


def test_envelope_hash_and_inner_sequence_are_verified(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (synthetic_invocation_started(),),
        expected_next_seq=1,
    )
    final = ledger.root / "0000000001-0000000001.json"
    document = json.loads(final.read_text(encoding="utf-8"))
    document[0]["event"]["entrypoint"] = "changed"
    final.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="digest mismatch"):
        ledger.read_all()


def test_inner_sequence_gap_is_rejected_even_with_valid_hashes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    events = (
        EventEnvelope.from_event(
            1,
            synthetic_invocation_started(),
        ),
        EventEnvelope.from_event(
            3,
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="task",
                payload=None,
            ),
        ),
    )
    document = [event.model_dump(mode="json") for event in events]
    (ledger.root / "0000000001-0000000003.json").write_bytes(
        canonical_json_bytes(document)  # type: ignore[arg-type]
    )
    with pytest.raises(LedgerIntegrityError, match="expected envelope sequence 2"):
        ledger.read_all()


def test_strict_json_decoder_restores_tuple_fields(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(),
            NodeActivated(
                activation_id="act-1",
                graph_instance_id="root",
                node_id="task",
                token_ids=(),
            ),
        ),
        expected_next_seq=1,
    )
    assert isinstance(ledger.read_all()[1].event, NodeActivated)


def test_event_models_are_frozen_strict_and_extra_forbidden() -> None:
    event = synthetic_invocation_started()
    with pytest.raises(ValidationError):
        InvocationStarted.model_validate(
            {
                "kind": "invocation_started",
                "invocation_id": "inv-1",
                "lock_digest": "a" * 64,
                "entrypoint": "main",
                "business": "no",
            }
        )
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({"seq": "1", "event": event, "event_sha256": "a" * 64})
    with pytest.raises(ValidationError):
        event.entrypoint = "changed"  # type: ignore[misc]


def _envelopes(*events: object) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.from_event(seq, event)  # type: ignore[arg-type]
        for seq, event in enumerate(events, start=1)
    )


def _workspace_identity(task_id: str = "task-1", *, attempt: int = 1) -> TaskWorkspaceIdentity:
    payload = {
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": f"attempt-{attempt}",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": canonical_digest({"task_id": task_id, "attempt": attempt, "kind": "write-root"}),
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def _staged_write_set(task_id: str = "task-1", *, attempt: int = 1) -> StagedWriteSet:
    identity = _workspace_identity(task_id, attempt=attempt)
    payload = {"identity_digest": identity.identity_digest, "files": []}
    return StagedWriteSet(
        identity_digest=identity.identity_digest,
        files=(),
        staged_digest=canonical_digest(payload),
    )


def _task_commit(
    activation_id: str,
    output: object,
    *,
    task_id: str = "task-1",
    attempt: int = 1,
    effect_ids: tuple[str, ...] = (),
) -> TaskCommitPrepared:
    identity = _workspace_identity(task_id, attempt=attempt)
    staged = _staged_write_set(task_id, attempt=attempt)
    return TaskCommitPrepared(
        task_id=task_id,
        activation_id=activation_id,
        attempt=attempt,
        output=output,  # type: ignore[arg-type]
        workspace_identity=identity,
        staged_write_set=staged,
        staged_write_set_digest=staged.staged_digest,
        effect_ids=effect_ids,
    )


def _task_promotion(
    activation_id: str,
    *,
    task_id: str = "task-1",
    attempt: int = 1,
    staged_write_set_digest: str | None = None,
) -> TaskPromotionCompleted:
    return TaskPromotionCompleted(
        task_id=task_id,
        activation_id=activation_id,
        attempt=attempt,
        staged_write_set_digest=(
            staged_write_set_digest or _staged_write_set(task_id, attempt=attempt).staged_digest
        ),
        promotion_receipt_digest="c" * 64,
    )


def _task_success(
    activation_id: str,
    output: object,
    *,
    task_id: str = "task-1",
    attempt: int = 1,
) -> TaskAttemptSucceeded:
    return TaskAttemptSucceeded(
        activation_id=activation_id,
        attempt=attempt,
        output=output,  # type: ignore[arg-type]
        staged_write_set_digest=_staged_write_set(task_id, attempt=attempt).staged_digest,
        promotion_receipt_digest="c" * 64,
    )


def _complete_task_history() -> tuple[EventEnvelope, ...]:
    return _envelopes(
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="task",
            payload={"input": True},
        ),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
        NodeActivated(
            activation_id="act-1",
            graph_instance_id="root",
            node_id="task",
            token_ids=("tok-1",),
        ),
        TaskAttemptStarted(
            activation_id="act-1",
            attempt=1,
            lease_expires_at="11",
        ),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="act-1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
        TaskLeaseHeartbeat(
            task_id="task-1",
            activation_id="act-1",
            attempt=1,
            owner_id="worker-1",
            heartbeat_at=5.0,
            expires_at=15.0,
        ),
        _task_commit("act-1", {"ok": True}),
        _task_promotion("act-1"),
        _task_success("act-1", {"ok": True}),
        NodeCompleted(activation_id="act-1", output={"ok": True}),
        GraphCompleted(graph_instance_id="root", output={"ok": True}),
        InvocationFinished(invocation_id="inv-1", status="succeeded"),
    )


@pytest.mark.parametrize(
    "batch_sizes",
    [
        (14,),
        (1,) * 14,
        (3, 4, 1, 2, 4),
    ],
)
def test_incremental_fold_matches_one_shot_across_batch_partitions(
    batch_sizes: tuple[int, ...],
) -> None:
    envelopes = _complete_task_history()
    cursor = FoldCursor()
    offset = 0

    for batch_size in batch_sizes:
        cursor = cursor.advance(envelopes[offset : offset + batch_size])
        offset += batch_size

    assert offset == len(envelopes)
    assert cursor.next_seq == len(envelopes) + 1
    assert cursor.projection == fold_events(envelopes)


def test_incremental_fold_rejects_batch_starting_at_wrong_sequence() -> None:
    envelopes = _complete_task_history()
    cursor = FoldCursor().advance(envelopes[:3])
    wrong_start = EventEnvelope.from_event(cursor.next_seq + 1, envelopes[3].event)

    with pytest.raises(ProjectionError, match="expected sequence 4, found 5"):
        cursor.advance((wrong_start,))

    assert cursor.projection == fold_events(envelopes[:3])
    assert cursor.next_seq == 4


def test_incremental_fold_rejects_digest_mismatch_without_partial_advance() -> None:
    envelopes = _complete_task_history()
    cursor = FoldCursor().advance(envelopes[:3])
    forged = envelopes[4].model_copy(update={"event_sha256": "0" * 64})

    with pytest.raises(ProjectionError, match="envelope digest mismatch"):
        cursor.advance((envelopes[3], forged))

    assert cursor.projection == fold_events(envelopes[:3])
    assert cursor.next_seq == 4


def test_fold_rejects_success_without_started_attempt() -> None:
    events = _envelopes(
        synthetic_invocation_started(),
        NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
        _task_success("act-1", None),
    )
    with pytest.raises(ProjectionError, match="without a matching active attempt"):
        fold_events(events)


def test_fold_rejects_consuming_a_token_twice() -> None:
    events = _envelopes(
        synthetic_invocation_started(),
        TokenOffered(token_id="tok-1", graph_instance_id="root", source=None, target="task", payload=None),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
    )
    with pytest.raises(ProjectionError, match="already consumed"):
        fold_events(events)


def test_fold_records_attempt_history() -> None:
    projection = fold_events(
        _envelopes(
            synthetic_invocation_started(),
            NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
            TaskAttemptStarted(activation_id="act-1", attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
            _task_commit("act-1", {"ok": True}),
            _task_promotion("act-1"),
            _task_success("act-1", {"ok": True}),
        )
    )
    assert projection.activations[0].attempts[0].status == "succeeded"


def test_checkpoint_document_requires_schema_version_2(tmp_path: Path) -> None:
    envelopes = _envelopes(synthetic_invocation_started())
    projection = fold_events(envelopes)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=1, ledger_envelopes=envelopes)
    loaded = load_checkpoint(path, ledger_envelopes=envelopes)
    assert loaded is not None
    assert loaded.schema_version == "2"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "2"

    missing = dict(document)
    missing.pop("schema_version")
    path.write_text(json.dumps(missing), encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=envelopes) is None

    path.write_text(json.dumps({**document, "schema_version": "1"}), encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=envelopes) is None


def test_checkpoint_round_trip_and_corruption_fallback(tmp_path: Path) -> None:
    envelopes = _envelopes(synthetic_invocation_started())
    projection = fold_events(envelopes)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=1, ledger_envelopes=envelopes)
    loaded = load_checkpoint(path, ledger_envelopes=envelopes)
    assert loaded is not None
    assert loaded.projection == projection
    assert loaded.last_seq == 1

    document = json.loads(path.read_text(encoding="utf-8"))
    document["projection"]["entrypoint"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=envelopes) is None


def test_checkpoint_missing_malformed_and_ahead_return_none(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    assert load_checkpoint(path, ledger_envelopes=()) is None
    path.write_text("{broken", encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=()) is None

    envelopes = _envelopes(synthetic_invocation_started())
    projection = fold_events(envelopes)
    write_checkpoint(path, projection, last_seq=1, ledger_envelopes=envelopes)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["last_seq"] = 2
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=envelopes) is None


def test_atomic_no_clobber_publication_has_one_cross_process_winner(tmp_path: Path) -> None:
    destination = tmp_path / "0000000001-0000000001.json"
    script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path
        from graph_engine.runtime.ledger import LedgerConflictError, _publish_no_clobber

        root, contender = Path(sys.argv[1]), sys.argv[2]
        pending = root / f".pending-{contender}.json"
        pending.write_bytes(contender.encode())
        with pending.open("rb") as stream:
            os.fsync(stream.fileno())
        (root / f"ready-{contender}").touch()
        while len(tuple(root.glob("ready-*"))) != 2:
            time.sleep(0.005)
        try:
            _publish_no_clobber(pending, root / "0000000001-0000000001.json")
        except LedgerConflictError:
            print("lost")
        else:
            print("won")
        """
    )
    contenders = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), value],
            text=True,
            stdout=subprocess.PIPE,
        )
        for value in ("first", "second")
    ]
    outcomes = [process.communicate(timeout=10)[0].strip() for process in contenders]
    assert sorted(outcomes) == ["lost", "won"]
    winner = destination.read_text(encoding="utf-8")
    assert winner in {"first", "second"}
    assert destination.read_text(encoding="utf-8") == winner


@pytest.mark.parametrize(
    ("boundary", "committed"),
    [
        ("lock_acquired", False),
        ("pending_fsynced", False),
        ("final_installed", True),
        ("directory_fsynced", True),
    ],
)
def test_append_recovers_after_subprocess_crash_boundary(
    tmp_path: Path, boundary: str, committed: bool
) -> None:
    root = tmp_path / "ledger"
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path
        import graph_engine.runtime.ledger as module
        from graph_engine.runtime.events import InvocationStarted
        from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed

        _seed = empty_invocation_seed()
        stop = sys.argv[2]
        module._append_boundary = lambda name: os._exit(91) if name == stop else None
        module.Ledger(Path(sys.argv[1])).append_batch((InvocationStarted(
            invocation_id="inv-1",
            lock_digest="a" * 64,
            entrypoint="main",
            event_schema_version="2",
            runtime_authorization_digest=EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
            root_input_digest=_seed.root_input_digest,
        ),), expected_next_seq=1)
        """
    )
    crashed = subprocess.run([sys.executable, "-c", script, str(root), boundary], check=False)
    assert crashed.returncode == 91

    ledger = Ledger(root)
    assert [item.seq for item in ledger.read_all()] == ([1] if committed else [])
    next_seq = 2 if committed else 1
    next_event = (
        TokenOffered(
            token_id="tok-2",
            graph_instance_id="root",
            source=None,
            target="end",
            payload=None,
        )
        if committed
        else synthetic_invocation_started(invocation_id="inv-2", lock_digest="b" * 64)
    )
    ledger.append_batch((next_event,), expected_next_seq=next_seq)
    assert ledger.read_all()[-1].seq == next_seq


def test_append_rejects_batch_crossing_filename_sequence_limit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    with pytest.raises(ValueError, match="maximum sequence"):
        ledger.append_batch(
            (
                synthetic_invocation_started(),
                synthetic_invocation_started(invocation_id="inv-2", lock_digest="b" * 64),
            ),
            expected_next_seq=MAX_SEQUENCE,
        )
    assert not ledger.root.exists()


def test_unknown_failure_kind_is_rejected_by_strict_json_decoder() -> None:
    raw = {
        "seq": 1,
        "event": {
            "kind": "task_attempt_failed",
            "activation_id": "act-1",
            "attempt": 1,
            "failure": {"kind": "product_specific", "message": "no"},
        },
        "event_sha256": "a" * 64,
    }
    with pytest.raises(ValidationError, match="failure.kind"):
        EventEnvelope.model_validate(raw, strict=True)


def test_json_values_are_recursively_frozen_without_source_aliases() -> None:
    source: dict[str, object] = {"nested": {"items": ["one", "two"]}}
    event = TokenOffered(
        token_id="tok-1",
        graph_instance_id="root",
        source=None,
        target="task",
        payload=source,
    )
    envelope = EventEnvelope.from_event(1, event)
    cast(list[str], cast(dict[str, object], source["nested"])["items"])[0] = "mutated"
    payload = cast(Mapping[str, object], event.payload)
    nested = cast(Mapping[str, object], payload["nested"])
    items = cast(tuple[object, ...], nested["items"])
    assert items == ("one", "two")
    with pytest.raises(TypeError):
        payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        items[0] = "changed"  # type: ignore[index]
    envelope_payload = cast(Mapping[str, object], cast(TokenOffered, envelope.event).payload)
    with pytest.raises(TypeError):
        envelope_payload["new"] = True  # type: ignore[index]
    assert envelope.has_valid_digest()


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("wrong_target", "does not target node"),
        ("cross_instance", "belongs to another graph instance"),
        ("reuse", "already claimed by activation"),
    ],
)
def test_tokens_are_bound_to_target_graph_and_one_activation(case: str, expected_message: str) -> None:
    consume_node = "other" if case == "wrong_target" else "task"
    activation_graph = "child" if case == "cross_instance" else "root"
    events: list[object] = [
        synthetic_invocation_started(),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="task",
            payload=None,
        ),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id=consume_node),
        NodeActivated(
            activation_id="act-1",
            graph_instance_id=activation_graph,
            node_id=consume_node,
            token_ids=("tok-1",),
        ),
    ]
    if case == "reuse":
        events.append(
            NodeActivated(
                activation_id="act-2",
                graph_instance_id="root",
                node_id="task",
                token_ids=("tok-1",),
            )
        )
    with pytest.raises(ProjectionError, match=expected_message):
        fold_events(_envelopes(*events))


def _attempt_history(status: str) -> list[object]:
    events: list[object] = [
        synthetic_invocation_started(),
        NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
        TaskAttemptStarted(activation_id="act-1", attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
    ]
    if status == "failed":
        events.append(
            TaskAttemptFailed(
                activation_id="act-1",
                attempt=1,
                failure=TaskFailure(kind="transient", message="retry"),
            )
        )
    elif status == "succeeded":
        events.extend(
            (
                _task_commit("act-1", None),
                _task_promotion("act-1"),
                _task_success("act-1", None),
            )
        )
    elif status == "stopped":
        events.append(TaskAttemptStopped(activation_id="act-1", attempt=1, reason="stop"))
    return events


@pytest.mark.parametrize(
    ("prior", "action", "allowed"),
    [
        ("running", "start", False),
        ("running", "outcome", True),
        ("running", "complete", False),
        ("running", "interrupt", False),
        ("failed", "start", True),
        ("failed", "outcome", False),
        ("failed", "complete", False),
        ("failed", "interrupt", False),
        ("succeeded", "start", False),
        ("succeeded", "outcome", False),
        ("succeeded", "complete", True),
        ("succeeded", "interrupt", False),
        ("stopped", "start", False),
        ("stopped", "outcome", False),
        ("stopped", "complete", False),
        ("stopped", "interrupt", False),
    ],
)
def test_attempt_and_node_transition_matrix(prior: str, action: str, allowed: bool) -> None:
    events = _attempt_history(prior)
    attempt = 2 if prior == "failed" and action == "start" else 1
    actions: dict[str, object] = {
        "start": TaskAttemptStarted(
            activation_id="act-1", attempt=attempt, lease_expires_at="2030-01-02T00:00:00Z"
        ),
        "outcome": TaskAttemptFailed(
            activation_id="act-1",
            attempt=attempt,
            failure=TaskFailure(kind="transient", message="outcome"),
        ),
        "complete": NodeCompleted(activation_id="act-1"),
        "interrupt": _interrupt_event(),
    }

    def operation() -> InvocationProjection:
        return fold_events(_envelopes(*events, actions[action]))

    if allowed:
        operation()
    else:
        with pytest.raises(ProjectionError):
            operation()


def test_succeeded_task_cannot_enter_interrupt_resume_lifecycle() -> None:
    events = _envelopes(
        *_attempt_history("succeeded"),
        _interrupt_event(),
        InterruptResumed(interrupt_id="int-1", action="continue", payload=None),
        NodeCompleted(activation_id="act-1"),
    )
    with pytest.raises(ProjectionError, match="task-attempt history"):
        fold_events(events)


def test_graph_completion_and_successful_invocation_require_settled_state() -> None:
    live_attempt = _envelopes(
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
        TaskAttemptStarted(activation_id="act-1", attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        GraphCompleted(graph_instance_id="root"),
    )
    with pytest.raises(ProjectionError, match="unsettled activation"):
        fold_events(live_attempt)

    running_graph = _envelopes(
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        InvocationFinished(invocation_id="inv-1", status="succeeded"),
    )
    with pytest.raises(ProjectionError, match="running graph"):
        fold_events(running_graph)


@pytest.mark.parametrize("consumed", [False, True])
def test_graph_completion_rejects_unactivated_token(consumed: bool) -> None:
    events: list[object] = [
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="task",
            payload=None,
        ),
    ]
    if consumed:
        events.append(TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"))
    events.append(GraphCompleted(graph_instance_id="root"))
    with pytest.raises(ProjectionError, match="unclaimed token"):
        fold_events(_envelopes(*events))


@pytest.mark.parametrize("consumed", [False, True])
def test_successful_invocation_rejects_token_outside_completed_work(consumed: bool) -> None:
    events: list[object] = [
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphCompleted(graph_instance_id="root"),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="ghost",
            source=None,
            target="task",
            payload=None,
        ),
    ]
    if consumed:
        events.append(TokenConsumed(token_id="tok-1", graph_instance_id="ghost", node_id="task"))
    events.append(InvocationFinished(invocation_id="inv-1", status="succeeded"))
    with pytest.raises(ProjectionError, match="unsettled token"):
        fold_events(_envelopes(*events))


def test_claimed_token_settles_with_completed_activation_and_graph() -> None:
    projection = fold_events(
        _envelopes(
            synthetic_invocation_started(),
            GraphStarted(graph_instance_id="root", graph_id="root"),
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="end",
                payload=None,
            ),
            TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="end"),
            NodeActivated(
                activation_id="act-1",
                graph_instance_id="root",
                node_id="end",
                token_ids=("tok-1",),
            ),
            NodeCompleted(activation_id="act-1"),
            GraphCompleted(graph_instance_id="root"),
            InvocationFinished(invocation_id="inv-1", status="succeeded"),
        )
    )
    assert projection.status == "succeeded"


def test_graph_scoped_activity_is_rejected_after_completion() -> None:
    events = _envelopes(
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphCompleted(graph_instance_id="root"),
        TokenOffered(token_id="tok-1", graph_instance_id="root", source=None, target="end", payload=None),
    )
    with pytest.raises(ProjectionError, match="already completed"):
        fold_events(events)


def test_success_failed_and_stopped_invocation_cleanup_semantics_are_explicit() -> None:
    succeeded = fold_events(
        _envelopes(
            synthetic_invocation_started(),
            GraphStarted(graph_instance_id="root", graph_id="root"),
            GraphCompleted(graph_instance_id="root"),
            InvocationFinished(invocation_id="inv-1", status="succeeded"),
        )
    )
    assert succeeded.status == "succeeded"

    failed_events = _attempt_history("failed")
    failed = fold_events(
        _envelopes(
            *failed_events,
            InvocationFinished(invocation_id="inv-1", status="failed"),
        )
    )
    assert failed.status == "failed"
    assert failed.activations[0].status == "active"

    stopped_events = _attempt_history("stopped")
    stopped = fold_events(
        _envelopes(
            *stopped_events,
            InvocationFinished(invocation_id="inv-1", status="stopped"),
        )
    )
    assert stopped.status == "stopped"
    assert stopped.activations[0].status == "stopped"


def test_success_terminal_reason_is_forbidden_by_event_fold_and_projection() -> None:
    with pytest.raises(ValidationError, match="successful invocation"):
        InvocationFinished(
            invocation_id="inv-1",
            status="succeeded",
            terminal_reason="forged-success-reason",
        )

    forged = InvocationFinished.model_construct(
        invocation_id="inv-1",
        status="succeeded",
        terminal_reason="forged-success-reason",
    )
    forged_json = forged.model_dump(mode="json")
    forged_envelope = EventEnvelope.model_construct(
        seq=4,
        event=forged,
        event_sha256=canonical_digest({"seq": 4, "event": forged_json}),
    )
    with pytest.raises(ProjectionError, match="successful invocation"):
        fold_events(
            (
                *_envelopes(
                    synthetic_invocation_started(),
                    GraphStarted(graph_instance_id="root", graph_id="root"),
                    GraphCompleted(graph_instance_id="root"),
                ),
                forged_envelope,
            )
        )

    completed_graph = GraphInstanceRecord(
        graph_instance_id="root",
        graph_id="root",
        parent_graph_instance_id=None,
        parent_node_id=None,
        status="completed",
    )
    with pytest.raises(ValidationError, match="successful projection"):
        InvocationProjection(
            status="succeeded",
            invocation_id="inv-1",
            lock_digest="a" * 64,
            entrypoint="main",
            graph_instances=(completed_graph,),
            terminal_reason="forged-success-reason",
        )


def test_success_without_terminal_reason_remains_valid() -> None:
    event = InvocationFinished(invocation_id="inv-1", status="succeeded")
    projection = fold_events(
        _envelopes(
            synthetic_invocation_started(),
            GraphStarted(graph_instance_id="root", graph_id="root"),
            GraphCompleted(graph_instance_id="root"),
            event,
        )
    )

    assert event.terminal_reason is None
    assert projection.status == "succeeded"
    assert projection.terminal_reason is None


def test_folded_projection_json_is_deeply_immutable() -> None:
    projection = fold_events(
        _envelopes(
            synthetic_invocation_started(),
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="end",
                payload={"nested": [1, 2]},
            ),
        )
    )
    payload = cast(Mapping[str, object], projection.offered_tokens[0].payload)
    with pytest.raises(TypeError):
        cast(tuple[object, ...], payload["nested"])[0] = 3  # type: ignore[index]


def test_projection_rejects_semantically_impossible_states() -> None:
    with pytest.raises(ValidationError, match="identity"):
        InvocationProjection(status="running")

    graph = GraphInstanceRecord(
        graph_instance_id="root",
        graph_id="root",
        parent_graph_instance_id=None,
        parent_node_id=None,
    )
    with pytest.raises(ValidationError, match="duplicate graph instance"):
        InvocationProjection(
            status="running",
            invocation_id="inv-1",
            lock_digest="a" * 64,
            entrypoint="main",
            graph_instances=(graph, graph),
        )
    with pytest.raises(ValidationError, match="dangling activation"):
        InvocationProjection(
            status="running",
            invocation_id="inv-1",
            lock_digest="a" * 64,
            entrypoint="main",
            pending_interrupt=PendingInterrupt(
                interrupt_id="int-1",
                activation_id="missing",
                graph_instance_id="root",
                reason="review",
                actions=("continue",),
                input=None,
            ),
        )
    completed_graph = graph.model_copy(update={"status": "completed"})
    with pytest.raises(ValidationError, match="unsettled token"):
        InvocationProjection(
            status="succeeded",
            invocation_id="inv-1",
            lock_digest="a" * 64,
            entrypoint="main",
            graph_instances=(completed_graph,),
            offered_tokens=(
                TokenRecord(
                    token_id="tok-1",
                    graph_instance_id="root",
                    source=None,
                    target="end",
                    payload=None,
                ),
            ),
        )


def test_checkpoint_rejects_unrelated_and_stale_authoritative_prefixes(tmp_path: Path) -> None:
    first = _envelopes(synthetic_invocation_started())
    first_projection = fold_events(first)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, first_projection, last_seq=1, ledger_envelopes=first)

    unrelated = _envelopes(synthetic_invocation_started(invocation_id="inv-2", lock_digest="b" * 64))
    assert load_checkpoint(path, ledger_envelopes=unrelated) is None

    stale = (
        *first,
        EventEnvelope.from_event(
            2,
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="end",
                payload=None,
            ),
        ),
    )
    assert load_checkpoint(path, ledger_envelopes=stale) is None

    stale_projection = fold_events(stale)
    write_checkpoint(path, stale_projection, last_seq=2, ledger_envelopes=stale)
    assert load_checkpoint(path, ledger_envelopes=first) is None


def test_checkpoint_rejects_impossible_or_mismatched_projection(tmp_path: Path) -> None:
    envelopes = _envelopes(synthetic_invocation_started())
    projection = fold_events(envelopes)
    path = tmp_path / "checkpoint.json"
    with pytest.raises(ValueError, match="does not match ledger prefix"):
        write_checkpoint(
            path,
            projection.model_copy(update={"invocation_id": "other"}),
            last_seq=1,
            ledger_envelopes=envelopes,
        )

    write_checkpoint(path, projection, last_seq=1, ledger_envelopes=envelopes)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["projection"]["invocation_id"] = None
    payload = {
        "last_seq": document["last_seq"],
        "ledger_prefix_sha256": document["ledger_prefix_sha256"],
        "projection": document["projection"],
    }
    document["digest"] = canonical_digest(payload)  # type: ignore[arg-type]
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_checkpoint(path, ledger_envelopes=envelopes) is None


def test_loaded_checkpoint_json_is_deeply_immutable(tmp_path: Path) -> None:
    envelopes = _envelopes(
        synthetic_invocation_started(),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="end",
            payload={"nested": [1]},
        ),
    )
    projection = fold_events(envelopes)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=2, ledger_envelopes=envelopes)
    loaded = load_checkpoint(path, ledger_envelopes=envelopes)
    assert loaded is not None
    payload = cast(Mapping[str, object], loaded.projection.offered_tokens[0].payload)
    with pytest.raises(TypeError):
        cast(tuple[object, ...], payload["nested"])[0] = 2  # type: ignore[index]


_LOCK = "a" * 64


def _effect_key(effect_id: str, kind: str, payload: Mapping[str, object]) -> str:
    return canonical_digest(
        {
            "lock_digest": _LOCK,
            "effect_id": effect_id,
            "kind": kind,
            "payload_digest": canonical_digest(cast(dict[str, object], dict(payload))),
        }
    )


_KEY1 = _effect_key("effect-1", "toy.audit", {"n": 1})
_KEY2 = _effect_key("effect-2", "toy.audit", {"n": 2})


def _running_task_prefix() -> tuple[object, ...]:
    return (
        synthetic_invocation_started(lock_digest=_LOCK),
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


def _prepared_commit() -> TaskCommitPrepared:
    return _task_commit(
        "a1",
        {"ok": True},
        effect_ids=("effect-1", "effect-2"),
    )


def _prepared_promotion() -> TaskPromotionCompleted:
    return _task_promotion("a1")


def _intent(effect_id: str, index: int, payload: Mapping[str, object], key: str) -> EffectIntentCommitted:
    return EffectIntentCommitted(
        effect_id=effect_id,
        activation_id="a1",
        attempt=1,
        index=index,
        effect_kind="toy.audit",
        payload=dict(payload),
        idempotency_key=key,
    )


def _committed_effect_history() -> tuple[object, ...]:
    return (
        *_running_task_prefix(),
        _prepared_commit(),
        _intent("effect-1", 0, {"n": 1}, _KEY1),
        _intent("effect-2", 1, {"n": 2}, _KEY2),
        _prepared_promotion(),
    )


def test_fold_keeps_task_pending_until_all_effect_receipts() -> None:
    envelopes = _envelopes(
        *_running_task_prefix(),
        _prepared_commit(),
        EffectIntentCommitted(
            effect_id="effect-1",
            activation_id="a1",
            attempt=1,
            index=0,
            effect_kind="toy.audit",
            payload={"n": 1},
            idempotency_key=_KEY1,
        ),
        EffectIntentCommitted(
            effect_id="effect-2",
            activation_id="a1",
            attempt=1,
            index=1,
            effect_kind="toy.audit",
            payload={"n": 2},
            idempotency_key=_KEY2,
        ),
        _prepared_promotion(),
    )
    projection = fold_events(envelopes)
    assert projection.activations[-1].attempts[-1].status == "effect_pending"
    assert tuple(effect.status for effect in projection.effects) == ("committed", "committed")


def test_fold_accepts_lease_heartbeat_while_effect_pending() -> None:
    envelopes = _envelopes(
        *_committed_effect_history(),
        TaskLeaseHeartbeat(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            owner_id="worker-1",
            heartbeat_at=5.0,
            expires_at=15.0,
        ),
    )
    projection = fold_events(envelopes)
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.status == "effect_pending"
    assert attempt.lease_heartbeat_at == 5.0
    assert attempt.lease_expires_at_value == 15.0
    assert tuple(effect.status for effect in projection.effects) == ("committed", "committed")


def test_fold_records_receipts_then_succeeds_the_effect_pending_attempt() -> None:
    envelopes = _envelopes(
        *_committed_effect_history(),
        EffectApplyStarted(effect_id="effect-1", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
        EffectApplyStarted(effect_id="effect-2", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-2", apply_attempt=1, receipt={"remote": 2}),
        _task_success("a1", {"ok": True}),
    )
    projection = fold_events(envelopes)
    assert projection.activations[-1].attempts[-1].status == "succeeded"
    assert tuple(effect.status for effect in projection.effects) == ("applied", "applied")
    prepared = projection.activations[-1].attempts[-1].prepared_commit
    assert prepared is not None
    assert prepared.staged_write_set_digest == _staged_write_set().staged_digest
    assert prepared.promotion_receipt_digest == "c" * 64


@pytest.mark.parametrize("partition", ["all", "one", "split"])
def test_incremental_effect_fold_matches_one_shot(partition: str) -> None:
    envelopes = _envelopes(
        *_committed_effect_history(),
        EffectApplyStarted(effect_id="effect-1", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
        EffectApplyStarted(effect_id="effect-2", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-2", apply_attempt=1, receipt={"remote": 2}),
        _task_success("a1", {"ok": True}),
    )
    if partition == "all":
        batch_sizes = (len(envelopes),)
    elif partition == "one":
        batch_sizes = (1,) * len(envelopes)
    else:
        batch_sizes = (7, 4, len(envelopes) - 11)
    cursor = FoldCursor()
    offset = 0
    for batch_size in batch_sizes:
        cursor = cursor.advance(envelopes[offset : offset + batch_size])
        offset += batch_size
    assert offset == len(envelopes)
    assert cursor.projection == fold_events(envelopes)


def test_fold_rejects_effect_intent_without_prepared_commit() -> None:
    with pytest.raises(ProjectionError, match="prepared commit"):
        fold_events(_envelopes(*_running_task_prefix(), _intent("effect-1", 0, {"n": 1}, _KEY1)))


def test_fold_rejects_wrong_effect_index_order() -> None:
    with pytest.raises(ProjectionError, match="index"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                _prepared_commit(),
                _intent("effect-2", 1, {"n": 2}, _KEY2),
            )
        )


def test_fold_rejects_wrong_effect_order() -> None:
    with pytest.raises(ProjectionError, match="effect id"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                _prepared_commit(),
                _intent("effect-2", 0, {"n": 2}, _KEY2),
            )
        )


def test_fold_rejects_wrong_effect_idempotency_key() -> None:
    with pytest.raises(ProjectionError, match="idempotency"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                _prepared_commit(),
                _intent("effect-1", 0, {"n": 1}, "c" * 64),
            )
        )


def test_fold_rejects_duplicate_effect_intent() -> None:
    with pytest.raises(ProjectionError, match="duplicate"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                _prepared_commit(),
                _intent("effect-1", 0, {"n": 1}, _KEY1),
                _intent("effect-1", 0, {"n": 1}, _KEY1),
            )
        )


def test_fold_rejects_duplicate_effect_receipt() -> None:
    with pytest.raises(ProjectionError, match="duplicate"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                EffectApplyStarted(effect_id="effect-1", apply_attempt=1),
                EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
                EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
            )
        )


def test_fold_rejects_effect_apply_before_prior_receipt() -> None:
    with pytest.raises(ProjectionError, match="prior receipt"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                EffectApplyStarted(effect_id="effect-2", apply_attempt=1),
            )
        )


def test_fold_rejects_effect_receipt_without_apply() -> None:
    with pytest.raises(ProjectionError, match="without apply"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
            )
        )


def test_fold_rejects_success_before_all_effect_receipts() -> None:
    with pytest.raises(ProjectionError, match="receipt"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                _task_success("a1", {"ok": True}),
            )
        )


def test_fold_marks_frontier_effect_permanently_failed_and_refuses_later_attempt() -> None:
    failure = TaskFailure(kind="external_effect", message="denied", retryable=False)
    committed_failure = TaskAttemptCommittedEffectFailed(
        activation_id="a1",
        attempt=1,
        failure=failure,
        staged_write_set_digest=_staged_write_set("task-1").staged_digest,
        promotion_receipt_digest="c" * 64,
    )
    envelopes = _envelopes(
        *_committed_effect_history(),
        committed_failure,
    )
    projection = fold_events(envelopes)
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    assert attempt.failure == failure
    assert tuple(effect.status for effect in projection.effects) == (
        "permanently_failed",
        "permanently_failed",
    )
    assert projection.effects[0].failure == failure
    assert projection.effects[1].failure == failure
    with pytest.raises(ProjectionError, match="cannot retry"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                committed_failure,
                TaskAttemptStarted(activation_id="a1", attempt=2, lease_expires_at="12"),
            )
        )


def test_fold_rejects_ordinary_failure_while_effect_pending() -> None:
    with pytest.raises(ProjectionError, match="ordinary attempt outcome cannot follow"):
        fold_events(
            _envelopes(
                *_committed_effect_history(),
                TaskAttemptFailed(
                    activation_id="a1",
                    attempt=1,
                    failure=TaskFailure(kind="external_effect", message="denied", retryable=True),
                ),
            )
        )


def test_fold_rejects_non_retryable_effect_failure_followed_by_retry() -> None:
    with pytest.raises(ProjectionError, match="non-retryable"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                TaskAttemptFailed(
                    activation_id="a1",
                    attempt=1,
                    failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
                ),
                TaskAttemptStarted(activation_id="a1", attempt=2, lease_expires_at="12"),
            )
        )


def test_fold_rejects_promotion_write_set_mismatch_for_prepared_commit() -> None:
    with pytest.raises(ProjectionError, match="promotion receipt"):
        fold_events(
            _envelopes(
                *_running_task_prefix(),
                _prepared_commit(),
                TaskPromotionCompleted(
                    task_id="task-1",
                    activation_id="a1",
                    attempt=1,
                    staged_write_set_digest="b" * 64,
                    promotion_receipt_digest="c" * 64,
                ),
            )
        )


@pytest.mark.parametrize(
    "terminal",
    [
        GraphCompleted(graph_instance_id="root"),
        GraphFailed(graph_instance_id="root", reason="forged"),
        InvocationFinished(invocation_id="inv-1", status="failed", terminal_reason="forged"),
    ],
)
def test_fold_rejects_graph_terminal_while_effects_remain_pending(terminal: object) -> None:
    with pytest.raises(ProjectionError, match="pending"):
        fold_events(_envelopes(*_committed_effect_history(), terminal))


def test_checkpoint_round_trips_effect_pending_projection(tmp_path: Path) -> None:
    envelopes = _envelopes(*_committed_effect_history())
    projection = fold_events(envelopes)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=len(envelopes), ledger_envelopes=envelopes)
    loaded = load_checkpoint(path, ledger_envelopes=envelopes)
    assert loaded is not None
    assert loaded.projection == projection
    assert loaded.projection.activations[-1].attempts[-1].status == "effect_pending"
    assert tuple(effect.status for effect in loaded.projection.effects) == ("committed", "committed")


def test_activity_events_round_trip_through_ledger_json(tmp_path: Path) -> None:
    fingerprint = {"endpoint": "https://127.0.0.1:1", "executable": "runner"}
    reference = {"id": "ext-1"}
    outcome = TaskOutcome.succeeded({"answer": 42})
    events = (
        TaskActivityPrepared(
            activity_id="activity-1",
            task_id="task-1",
            activation_id="act-1",
            attempt=1,
            request_digest="0" * 64,
            workspace_identity=_workspace_identity(),
        ),
        TaskActivityDispatchStarted(
            activity_id="activity-1",
            ordinal=1,
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=canonical_digest(fingerprint),
        ),
        TaskActivityBound(
            activity_id="activity-1",
            reference=reference,
            reference_digest=canonical_digest(reference),
        ),
        TaskActivityCancelRequested(
            activity_id="activity-1",
            reason="timeout",
            requested_at=1.0,
        ),
        TaskActivityTerminalObserved(
            activity_id="activity-1",
            outcome=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
            staged_write_set_digest=_staged_write_set().staged_digest,
            promotion_receipt_digest="c" * 64,
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
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(events, expected_next_seq=1)
    restored = tuple(item.event for item in ledger.read_all())
    assert restored == events


def test_checkpoint_round_trips_bound_activity_projection(tmp_path: Path) -> None:
    fingerprint = {"endpoint": "https://127.0.0.1:1", "executable": "runner"}
    reference = {"id": "ext-1"}
    envelopes = _envelopes(
        *_running_task_prefix(),
        TaskActivityPrepared(
            activity_id="activity-1",
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            request_digest="0" * 64,
            workspace_identity=_workspace_identity(),
        ),
        TaskActivityDispatchStarted(
            activity_id="activity-1",
            ordinal=1,
            dispatch_fingerprint=fingerprint,
            dispatch_fingerprint_digest=canonical_digest(fingerprint),
        ),
        TaskActivityBound(
            activity_id="activity-1",
            reference=reference,
            reference_digest=canonical_digest(reference),
        ),
    )
    projection = fold_events(envelopes)
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.state == "bound"
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=len(envelopes), ledger_envelopes=envelopes)
    loaded = load_checkpoint(path, ledger_envelopes=envelopes)
    assert loaded is not None
    assert loaded.projection == projection
    assert loaded.projection.activations[-1].attempts[-1].activity == attempt.activity


EventIDValue = str | tuple[str, ...] | None
EventIDFields = tuple[tuple[str, EventIDValue], ...]
EventIDSignature = tuple[int, str, EventIDFields]

_ID_FIELDS_BY_EVENT_KIND: dict[str, tuple[str, ...]] = {
    "invocation_started": ("invocation_id",),
    "graph_started": (
        "graph_instance_id",
        "graph_id",
        "parent_graph_instance_id",
        "parent_node_id",
        "parent_activation_id",
    ),
    "token_offered": ("token_id", "graph_instance_id"),
    "token_consumed": ("token_id", "graph_instance_id", "node_id"),
    "node_activated": ("activation_id", "graph_instance_id", "node_id", "token_ids"),
    "task_attempt_started": ("activation_id",),
    "task_lease_acquired": ("task_id", "activation_id", "owner_id"),
    "task_lease_heartbeat": ("task_id", "activation_id", "owner_id"),
    "task_activity_prepared": ("activity_id", "task_id", "activation_id"),
    "task_activity_dispatch_started": ("activity_id",),
    "task_activity_bound": ("activity_id",),
    "task_activity_cancel_requested": ("activity_id",),
    "task_activity_terminal_observed": ("activity_id",),
    "task_lease_adopted": ("activity_id", "task_id", "activation_id", "owner_id"),
    "task_commit_prepared": ("task_id", "activation_id", "effect_ids"),
    "task_promotion_completed": ("task_id", "activation_id"),
    "effect_intent_committed": ("effect_id", "activation_id"),
    "effect_apply_started": ("effect_id",),
    "effect_receipt_recorded": ("effect_id",),
    "task_attempt_succeeded": ("activation_id",),
    "task_attempt_committed_effect_failed": ("activation_id",),
    "task_attempt_failed": ("activation_id",),
    "task_attempt_stopped": ("activation_id",),
    "node_completed": ("activation_id",),
    "node_failed": ("activation_id",),
    "node_interrupted": ("activation_id", "interrupt_id", "graph_instance_id"),
    "interrupt_resumed": ("interrupt_id",),
    "graph_completed": ("graph_instance_id",),
    "graph_failed": ("graph_instance_id",),
    "invocation_finished": ("invocation_id",),
}


def _runtime_event_types() -> tuple[type, ...]:
    from typing import get_args

    from graph_engine.runtime.events import RuntimeEvent, RuntimeEventModel

    event_union = get_args(RuntimeEvent)[0]
    return cast(tuple[type, ...], get_args(event_union))


def _runtime_event_id_schema() -> dict[str, tuple[str, ...]]:
    schema: dict[str, tuple[str, ...]] = {}
    for event_type in _runtime_event_types():
        event_kind = cast(str, event_type.model_fields["kind"].default)
        schema[event_kind] = tuple(
            field_name for field_name in event_type.model_fields if field_name.endswith(("_id", "_ids"))
        )
    return schema


def _event_id_fields(event: object) -> EventIDFields:
    kind = cast(str, getattr(event, "kind"))
    try:
        field_names = _ID_FIELDS_BY_EVENT_KIND[kind]
    except KeyError as error:
        raise AssertionError(f"event ID schema does not cover {kind!r}") from error
    return tuple((field_name, cast(EventIDValue, getattr(event, field_name))) for field_name in field_names)


def _event_id_sequence(envelopes: tuple[EventEnvelope, ...]) -> tuple[EventIDSignature, ...]:
    return tuple((envelope.seq, envelope.event.kind, _event_id_fields(envelope.event)) for envelope in envelopes)


def test_event_id_projection_preserves_parent_and_complete_token_bindings() -> None:
    parent_activation_id = "parent-activation"
    child_graph_id = canonical_digest(
        {"parent_activation_id": parent_activation_id, "graph_id": "child"}
    )
    envelopes = (
        EventEnvelope.from_event(
            1,
            GraphStarted(
                graph_instance_id=child_graph_id,
                graph_id="child",
                parent_graph_instance_id="root",
                parent_node_id="child-subgraph",
                parent_activation_id=parent_activation_id,
            ),
        ),
        EventEnvelope.from_event(
            2,
            NodeActivated(
                activation_id="joined-activation",
                graph_instance_id="root",
                node_id="joined",
                token_ids=("left-token", "child-token"),
            ),
        ),
    )
    assert _event_id_sequence(envelopes) == (
        (
            1,
            "graph_started",
            (
                ("graph_instance_id", child_graph_id),
                ("graph_id", "child"),
                ("parent_graph_instance_id", "root"),
                ("parent_node_id", "child-subgraph"),
                ("parent_activation_id", parent_activation_id),
            ),
        ),
        (
            2,
            "node_activated",
            (
                ("activation_id", "joined-activation"),
                ("graph_instance_id", "root"),
                ("node_id", "joined"),
                ("token_ids", ("left-token", "child-token")),
            ),
        ),
    )


def test_event_id_projection_covers_every_runtime_event_id_field() -> None:
    assert _ID_FIELDS_BY_EVENT_KIND == _runtime_event_id_schema()
