from pathlib import Path
import json

import pytest
from pydantic import ValidationError

from graph_engine.canonical import canonical_json_bytes
from graph_engine.runtime.checkpoint import load_checkpoint, write_checkpoint
from graph_engine.runtime.events import (
    EventEnvelope,
    InvocationStarted,
    NodeActivated,
    TaskAttemptStarted,
    TaskAttemptSucceeded,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import Ledger, LedgerConflictError, LedgerIntegrityError
from graph_engine.runtime.models import ProjectionError, fold_events


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
    projection = fold_events(
        _envelopes(InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"))
    )
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, projection, last_seq=1)
    loaded = load_checkpoint(path, ledger_last_seq=1)
    assert loaded is not None
    assert loaded.projection == projection
    assert loaded.last_seq == 1

    document = json.loads(path.read_text(encoding="utf-8"))
    document["projection"]["entrypoint"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_checkpoint(path, ledger_last_seq=1) is None


def test_checkpoint_missing_malformed_and_ahead_return_none(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    assert load_checkpoint(path, ledger_last_seq=0) is None
    path.write_text("{broken", encoding="utf-8")
    assert load_checkpoint(path, ledger_last_seq=0) is None

    projection = fold_events(
        _envelopes(InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"))
    )
    write_checkpoint(path, projection, last_seq=1)
    assert load_checkpoint(path, ledger_last_seq=0) is None
