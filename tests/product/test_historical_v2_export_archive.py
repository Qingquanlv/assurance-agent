from __future__ import annotations

import hashlib
import json
from pathlib import Path

from graph_engine.composition import InvocationLock
from graph_engine.evidence.legacy_v2 import (
    authenticate_invocation_lock_v2,
    fold_legacy_events,
    read_legacy_ledger,
)
from graph_engine.runtime.events import GraphCompleted, GraphStarted, InvocationFinished, InvocationStarted
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed

from tests.product.test_result_export import CHANGE_ID, write_achieved

_GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
)


def _golden_bytes() -> bytes:
    return _GOLDEN.read_text(encoding="utf-8").strip().encode()


def test_historical_lock_show_document_matches_original_v2_authentication() -> None:
    raw = _golden_bytes()
    lock = authenticate_invocation_lock_v2(raw)
    original = InvocationLock.model_validate(
        {**json.loads(raw), "canonical_bytes": raw, "digest": hashlib.sha256(raw).hexdigest()}
    )
    document = {
        "lock_digest": lock.digest,
        "engine_api": lock.engine_api,
        "lock": json.loads(lock.canonical_bytes.decode("utf-8")),
    }
    assert document["lock_digest"] == original.digest
    assert document["engine_api"] == original.engine_api
    assert document["lock"]["schema_version"] == "2"
    assert "compiled_workflow" in document["lock"]
    assert document["lock"] == json.loads(original.canonical_bytes.decode("utf-8"))


def test_historical_ledger_fold_matches_original_export_inputs(tmp_path: Path) -> None:
    seed = empty_invocation_seed()
    started = InvocationStarted(
        invocation_id="inv-historical-export",
        lock_digest="a" * 64,
        entrypoint="archive",
        event_schema_version="2",
        runtime_authorization_digest=EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
        root_input_digest=seed.root_input_digest,
    )
    finished = InvocationFinished(invocation_id=started.invocation_id, status="succeeded")
    Ledger(tmp_path / "ledger").append_batch(
        (
            started,
            GraphStarted(graph_instance_id="root", graph_id="root"),
            GraphCompleted(graph_instance_id="root"),
            finished,
        ),
        expected_next_seq=1,
    )
    envelopes = read_legacy_ledger(tmp_path / "ledger")
    assert fold_legacy_events(envelopes) == fold_events(envelopes)
    assert envelopes[0].event.root_input_digest == seed.root_input_digest  # type: ignore[union-attr]


def test_historical_export_and_archive_still_work_on_legacy_era_change(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    exported = cli_runner.invoke(
        app,
        ["export", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert exported.exit_code == 0, exported.output
    receipt = publish_achieved(project, CHANGE_ID)
    assert receipt.change_id == CHANGE_ID
    archived = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert archived.exit_code == 0, archived.output
