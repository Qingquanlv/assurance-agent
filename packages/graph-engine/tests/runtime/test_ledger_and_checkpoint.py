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
from graph_engine.plugin_api import TaskFailure
from graph_engine.runtime.checkpoint import load_checkpoint, write_checkpoint
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    NodeInterrupted,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import MAX_SEQUENCE, Ledger, LedgerConflictError, LedgerIntegrityError
from graph_engine.runtime.models import (
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
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
            (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
            expected_next_seq=0,
        )


def test_append_rejects_stale_writer_without_changing_final_batch(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
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
        (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
        expected_next_seq=1,
    )
    append = getattr(ledger_runtime, "append_validated_batch", None)
    assert callable(append), "runtime ledger must provide the centralized append protocol"

    with pytest.raises(ProjectionError):
        append(
            ledger,
            (
                InvocationStarted(
                    invocation_id="inv-2",
                    product_digest="b" * 64,
                    entrypoint="main",
                ),
            ),
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
            (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
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
        (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
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
            (
                InvocationStarted(
                    invocation_id="inv-1",
                    product_digest="a" * 64,
                    entrypoint="main",
                ),
            ),
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
            (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
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
        (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
        expected_next_seq=1,
    )
    source = ledger.root / "0000000001-0000000001.json"
    source.rename(ledger.root / "0000000001-0000000002.json")
    with pytest.raises(LedgerIntegrityError, match="does not match envelope range"):
        ledger.read_all()


def test_envelope_hash_and_inner_sequence_are_verified(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),),
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
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
    event = InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main")
    with pytest.raises(ValidationError):
        InvocationStarted.model_validate(
            {
                "kind": "invocation_started",
                "invocation_id": "inv-1",
                "product_digest": "a" * 64,
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


def test_fold_rejects_success_without_started_attempt() -> None:
    events = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
        TaskAttemptSucceeded(activation_id="act-1", attempt=1, output=None),
    )
    with pytest.raises(ProjectionError, match="without a matching active attempt"):
        fold_events(events)


def test_fold_rejects_consuming_a_token_twice() -> None:
    events = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        TokenOffered(token_id="tok-1", graph_instance_id="root", source=None, target="task", payload=None),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
    )
    with pytest.raises(ProjectionError, match="already consumed"):
        fold_events(events)


def test_fold_records_attempt_history() -> None:
    projection = fold_events(
        _envelopes(
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
            NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
            TaskAttemptStarted(activation_id="act-1", attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
            TaskAttemptSucceeded(activation_id="act-1", attempt=1, output={"ok": True}),
        )
    )
    assert projection.activations[0].attempts[0].status == "succeeded"


def test_checkpoint_round_trip_and_corruption_fallback(tmp_path: Path) -> None:
    envelopes = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main")
    )
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

    envelopes = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main")
    )
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

        stop = sys.argv[2]
        module._append_boundary = lambda name: os._exit(91) if name == stop else None
        module.Ledger(Path(sys.argv[1])).append_batch((InvocationStarted(
            invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"
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
        else InvocationStarted(invocation_id="inv-2", product_digest="b" * 64, entrypoint="main")
    )
    ledger.append_batch((next_event,), expected_next_seq=next_seq)
    assert ledger.read_all()[-1].seq == next_seq


def test_append_rejects_batch_crossing_filename_sequence_limit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    with pytest.raises(ValueError, match="maximum sequence"):
        ledger.append_batch(
            (
                InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
                InvocationStarted(invocation_id="inv-2", product_digest="b" * 64, entrypoint="main"),
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
        events.append(TaskAttemptSucceeded(activation_id="act-1", attempt=1, output=None))
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
        "outcome": TaskAttemptSucceeded(activation_id="act-1", attempt=attempt, output=None),
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        NodeActivated(activation_id="act-1", graph_instance_id="root", node_id="task", token_ids=()),
        TaskAttemptStarted(activation_id="act-1", attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        GraphCompleted(graph_instance_id="root"),
    )
    with pytest.raises(ProjectionError, match="unsettled activation"):
        fold_events(live_attempt)

    running_graph = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        InvocationFinished(invocation_id="inv-1", status="succeeded"),
    )
    with pytest.raises(ProjectionError, match="running graph"):
        fold_events(running_graph)


@pytest.mark.parametrize("consumed", [False, True])
def test_graph_completion_rejects_unactivated_token(consumed: bool) -> None:
    events: list[object] = [
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphCompleted(graph_instance_id="root"),
        TokenOffered(token_id="tok-1", graph_instance_id="root", source=None, target="end", payload=None),
    )
    with pytest.raises(ProjectionError, match="already completed"):
        fold_events(events)


def test_success_failed_and_stopped_invocation_cleanup_semantics_are_explicit() -> None:
    succeeded = fold_events(
        _envelopes(
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
                    InvocationStarted(
                        invocation_id="inv-1",
                        product_digest="a" * 64,
                        entrypoint="main",
                    ),
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
            product_digest="a" * 64,
            entrypoint="main",
            graph_instances=(completed_graph,),
            terminal_reason="forged-success-reason",
        )


def test_success_without_terminal_reason_remains_valid() -> None:
    event = InvocationFinished(invocation_id="inv-1", status="succeeded")
    projection = fold_events(
        _envelopes(
            InvocationStarted(
                invocation_id="inv-1",
                product_digest="a" * 64,
                entrypoint="main",
            ),
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
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
            product_digest="a" * 64,
            entrypoint="main",
            graph_instances=(graph, graph),
        )
    with pytest.raises(ValidationError, match="dangling activation"):
        InvocationProjection(
            status="running",
            invocation_id="inv-1",
            product_digest="a" * 64,
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
            product_digest="a" * 64,
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
    first = _envelopes(InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"))
    first_projection = fold_events(first)
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, first_projection, last_seq=1, ledger_envelopes=first)

    unrelated = _envelopes(
        InvocationStarted(invocation_id="inv-2", product_digest="b" * 64, entrypoint="main")
    )
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
    envelopes = _envelopes(
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main")
    )
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
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
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
