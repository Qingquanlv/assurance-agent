from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from graph_engine.composition import InvocationLock
from graph_engine.evidence.legacy_v2 import (
    LegacyEvidenceError,
    authenticate_invocation_lock_v2,
    fold_legacy_events,
    read_legacy_ledger,
)
from graph_engine.evidence.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    InvocationFinished,
    InvocationStarted,
)
from graph_engine.evidence.ledger import Ledger, LedgerPublicationIndeterminate
from graph_engine.evidence.models import fold_events
from graph_engine.evidence.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed

_GOLDEN = Path(__file__).resolve().parents[1] / "composition" / "invocation-lock-v2.golden.json"
_FORBIDDEN_NAMES = {
    "append",
    "append_batch",
    "append_validated_batch",
    "resume",
    "plan_next",
    "scheduler",
    "settle",
    "effect_settlement",
}


def _golden_bytes() -> bytes:
    return _GOLDEN.read_text(encoding="utf-8").strip().encode()


def _terminal_events() -> tuple[InvocationStarted, GraphStarted, GraphCompleted, InvocationFinished]:
    started = _started()
    return (
        started,
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphCompleted(graph_instance_id="root"),
        InvocationFinished(invocation_id=started.invocation_id, status="succeeded"),
    )


def _started() -> InvocationStarted:
    seed = empty_invocation_seed()
    return InvocationStarted(
        invocation_id="inv-1",
        lock_digest="a" * 64,
        entrypoint="main",
        event_schema_version="2",
        runtime_authorization_digest=EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
        root_input_digest=seed.root_input_digest,
    )


def test_reader_module_exposes_no_append_or_scheduler_api() -> None:
    source = (Path(__file__).resolve().parents[2] / "graph_engine" / "evidence" / "legacy_v2.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    assert defined.isdisjoint(_FORBIDDEN_NAMES)
    exported = ast.parse(source)
    assigns = [
        node
        for node in exported.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    assert assigns
    names = {elt.value for assign in assigns for elt in assign.value.elts}  # type: ignore[union-attr]
    assert names.isdisjoint(_FORBIDDEN_NAMES)
    assert "authenticate_invocation_lock_v2" in names
    assert "read_legacy_ledger" in names
    assert "fold_legacy_events" in names


def test_golden_lock_authenticates_byte_exact_against_original() -> None:
    raw = _golden_bytes()
    lock = authenticate_invocation_lock_v2(raw)
    original = InvocationLock.model_validate(
        {**json.loads(raw), "canonical_bytes": raw, "digest": hashlib.sha256(raw).hexdigest()}
    )
    assert lock.digest == original.digest
    assert lock.canonical_bytes == raw
    assert lock.canonical_bytes == original.canonical_bytes
    assert lock.schema_version == "2"


def test_tampered_lock_bytes_are_rejected() -> None:
    raw = bytearray(_golden_bytes())
    raw[-2] = 48 if raw[-2] != 48 else 49
    with pytest.raises(LegacyEvidenceError, match="invocation lock"):
        authenticate_invocation_lock_v2(bytes(raw))


def test_legacy_ledger_read_and_fold_match_original(tmp_path: Path) -> None:
    events = _terminal_events()
    ledger = Ledger(tmp_path / "ledger")
    published = ledger.append_batch(events, expected_next_seq=1)
    envelopes = read_legacy_ledger(tmp_path / "ledger")
    assert envelopes == published
    assert envelopes == Ledger(tmp_path / "ledger").read_all()
    projection = fold_legacy_events(envelopes)
    assert projection == fold_events(envelopes)
    assert projection.status == "succeeded"
    assert projection.invocation_id == events[0].invocation_id


def test_leftover_pending_batch_is_publication_indeterminate(tmp_path: Path) -> None:
    started = _started()
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch((started,), expected_next_seq=1)
    (tmp_path / "ledger" / ".pending-drain.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(LedgerPublicationIndeterminate, match="indeterminate"):
        read_legacy_ledger(tmp_path / "ledger")


def test_tampered_legacy_ledger_is_rejected(tmp_path: Path) -> None:
    started = _started()
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch((started,), expected_next_seq=1)
    batch = next(path for path in (tmp_path / "ledger").iterdir() if path.suffix == ".json")
    payload = json.loads(batch.read_text(encoding="utf-8"))
    payload[0]["digest"] = "0" * 64
    batch.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="digest"):
        read_legacy_ledger(tmp_path / "ledger")


def test_fold_rejects_tampered_envelope_digest() -> None:
    started = _started()
    envelope = EventEnvelope.from_event(1, started)
    tampered = envelope.model_copy(update={"event_sha256": "0" * 64})
    with pytest.raises(Exception, match="digest"):
        fold_legacy_events((tampered,))
