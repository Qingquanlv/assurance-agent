from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_outbox import ImprovementOutboxEntry
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.retro.fallback import materialize_pipeline_failure_fallback
from assurance_agent.workflow.improvements.events import read_improvement_events
from assurance_agent.workflow.improvements.outbox import (
    drain_reconcile_outbox,
    enqueue_reconcile,
)


def _entry(tmp_path: Path, retro_id: str, stage: str) -> ImprovementOutboxEntry:
    failure = RetroPipelineFailure(
        failure_id=f"FAIL-{retro_id}",
        retro_id=retro_id,
        stage=stage,
        error_kind="internal",
        message_fingerprint="sha256:message",
        occurred_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    context, candidate = materialize_pipeline_failure_fallback(tmp_path, failure=failure, batch_scope=None)
    return ImprovementOutboxEntry(
        retro_id=retro_id,
        candidate_sha256=sha256_bytes(canonical_json_bytes(candidate)),
        context_sha256=sha256_bytes(canonical_json_bytes(context)),
        context=context,
        candidate=candidate,
        pipeline_failure=failure,
    )


def test_enqueue_is_atomic_and_idempotent(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "retro-1", "collect")
    first = enqueue_reconcile(tmp_path, entry)
    second = enqueue_reconcile(tmp_path, entry)
    assert first == second
    assert ImprovementOutboxEntry.model_validate_json(first.read_text()) == entry
    assert not tuple(first.parent.glob("*.tmp"))


def test_drain_replay_after_commit_before_cleanup_appends_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = enqueue_reconcile(tmp_path, _entry(tmp_path, "retro-1", "collect"))
    original_unlink = Path.unlink
    failed = False

    def fail_pending_once(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal failed
        if path == pending and not failed:
            failed = True
            raise OSError("crash before pending cleanup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_once)
    with pytest.raises(OSError, match="cleanup"):
        drain_reconcile_outbox(tmp_path)
    monkeypatch.setattr(Path, "unlink", original_unlink)

    statuses = drain_reconcile_outbox(tmp_path)

    assert len(statuses) == 1
    events = read_improvement_events(tmp_path / "qa/improvements/events.jsonl")
    assert len(events) == 1
    assert not pending.exists()


def test_drain_processes_pending_filenames_in_stable_order(tmp_path: Path) -> None:
    enqueue_reconcile(tmp_path, _entry(tmp_path, "retro-z", "collect"))
    enqueue_reconcile(tmp_path, _entry(tmp_path, "retro-a", "assemble"))
    pending = tmp_path / "qa/improvements/outbox/pending"
    expected = tuple(
        ImprovementOutboxEntry.model_validate_json(path.read_text()).retro_id
        for path in sorted(pending.glob("*.json"), key=lambda item: item.name)
    )

    statuses = drain_reconcile_outbox(tmp_path)

    assert tuple(status.retro_id for status in statuses) == expected
