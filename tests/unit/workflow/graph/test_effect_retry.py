"""Independent effect-retry sidecar and root terminal fence."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.effect_retry import (
    EffectRetryStateV1,
    EffectRetryStore,
    RootEffectFenceStore,
    RootTerminalFenceError,
    capped_backoff_seconds,
    format_rfc3339_z,
    parse_rfc3339_z,
)


def test_parse_rfc3339_z_rejects_offset_form() -> None:
    with pytest.raises(ValueError, match="RFC 3339"):
        parse_rfc3339_z("2026-08-01T00:00:00+00:00")
    assert parse_rfc3339_z("2026-08-01T00:00:00Z") == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_capped_deterministic_backoff() -> None:
    assert capped_backoff_seconds(1) == 1.0
    assert capped_backoff_seconds(2) == 2.0
    assert capped_backoff_seconds(3) == 4.0
    assert capped_backoff_seconds(10) == 60.0


def test_sidecar_advances_when_due_and_suppresses_before_due(tmp_path: Path) -> None:
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    first = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-1",
        kind="test_marker/v1",
        lock_key="effect:effect-1",
        error_code="progression_lock_timeout",
        now=now,
    )
    assert first is not None
    assert first.ordinal == 1
    # Before due: suppress (return same state, no ordinal advance).
    again = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-1",
        kind="test_marker/v1",
        lock_key="effect:effect-1",
        error_code="progression_lock_timeout",
        now=now + timedelta(seconds=0.1),
        expected=first,
    )
    assert again is not None
    assert again.ordinal == 1
    due = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-1",
        kind="test_marker/v1",
        lock_key="effect:effect-1",
        error_code="progression_lock_timeout",
        now=parse_rfc3339_z(first.next_retry_at) + timedelta(seconds=1),
        expected=first,
    )
    assert due is not None
    assert due.ordinal == 2


def test_concurrent_cas_one_winner(tmp_path: Path) -> None:
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    base = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-cas",
        kind="test_marker/v1",
        lock_key="effect:effect-cas",
        error_code="retryable_io",
        now=now,
    )
    assert base is not None
    due_at = parse_rfc3339_z(base.next_retry_at) + timedelta(seconds=1)
    results: list[EffectRetryStateV1 | None] = []

    def worker() -> None:
        results.append(
            store.schedule_next(
                fence_store=fence,
                root_invocation_id="root",
                invocation_id="inv",
                task_id="task",
                attempt_id="att",
                effect_id="effect-cas",
                kind="test_marker/v1",
                lock_key="effect:effect-cas",
                error_code="retryable_io",
                now=due_at,
                expected=base,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in results if item is not None and item.ordinal == 2]
    losers = [item for item in results if item is None]
    assert len(winners) == 1
    assert len(losers) == 3


def test_clear_if_acknowledged_makes_sidecar_inert(tmp_path: Path) -> None:
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    state = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-ack",
        kind="test_marker/v1",
        lock_key="effect:effect-ack",
        error_code="retryable_io",
        now=now,
    )
    assert state is not None
    store.clear_if_acknowledged("effect-ack")
    assert store.load("effect-ack") is None


def test_prepared_fence_suppresses_retry_committed_rejects_abort_restores(tmp_path: Path) -> None:
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fence.prepare_terminal("root", now=now)
    with pytest.raises(RootTerminalFenceError, match="prepared"):
        store.schedule_next(
            fence_store=fence,
            root_invocation_id="root",
            invocation_id="inv",
            task_id="task",
            attempt_id="att",
            effect_id="effect-fence",
            kind="test_marker/v1",
            lock_key="effect:effect-fence",
            error_code="retryable_io",
            now=now,
        )
    fence.abort_prepared("root")
    created = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-fence",
        kind="test_marker/v1",
        lock_key="effect:effect-fence",
        error_code="retryable_io",
        now=now,
    )
    assert created is not None
    fence.prepare_terminal("root", now=now)
    fence.commit_terminal("root", now=now + timedelta(seconds=1))
    with pytest.raises(RootTerminalFenceError, match="committed"):
        store.schedule_next(
            fence_store=fence,
            root_invocation_id="root",
            invocation_id="inv",
            task_id="task",
            attempt_id="att",
            effect_id="effect-fence-2",
            kind="test_marker/v1",
            lock_key="effect:effect-fence-2",
            error_code="retryable_io",
            now=now,
        )


def test_guard_acquired_before_schedule(tmp_path: Path) -> None:
    """schedule_next takes root guard; concurrent prepare cannot interleave mid-write."""
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    errors: list[BaseException] = []

    def schedule() -> None:
        try:
            store.schedule_next(
                fence_store=fence,
                root_invocation_id="root",
                invocation_id="inv",
                task_id="task",
                attempt_id="att",
                effect_id="effect-race",
                kind="test_marker/v1",
                lock_key="effect:effect-race",
                error_code="retryable_io",
                now=now,
            )
        except BaseException as exc:  # noqa: BLE001 — collect race outcomes
            errors.append(exc)

    def prepare() -> None:
        try:
            fence.prepare_terminal("root", now=now)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=schedule), threading.Thread(target=prepare)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Exactly one terminal outcome: either sidecar exists and fence prepared after,
    # or fence prepared and schedule raised prepared error.
    state = store.load("effect-race")
    fence_state = fence.load("root")
    assert state is not None or fence_state is not None
    if fence_state is not None and fence_state.status == "prepared" and state is None:
        assert any(isinstance(exc, RootTerminalFenceError) for exc in errors)


def test_format_round_trip() -> None:
    stamp = format_rfc3339_z(datetime(2026, 8, 1, 12, 30, 0, tzinfo=timezone.utc))
    assert stamp.endswith("Z")
    assert parse_rfc3339_z(stamp).year == 2026


def test_retry_sidecar_while_progression_lock_held(tmp_path: Path) -> None:
    """Sidecar advances independently while the progression lock is held."""
    from assurance_agent.workflow.core.progression import thread_lock_for

    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    lock = thread_lock_for(change)
    assert lock.acquire(blocking=False)
    try:
        first = store.schedule_next(
            fence_store=fence,
            root_invocation_id="root",
            invocation_id="inv",
            task_id="task",
            attempt_id="att",
            effect_id="effect-lock",
            kind="test_marker/v1",
            lock_key="effect:effect-lock",
            error_code="progression_lock_timeout",
            now=now,
        )
        assert first is not None
        assert first.ordinal == 1
        # Second "runtime" while lock still held: suppressed before due.
        held = store.schedule_next(
            fence_store=fence,
            root_invocation_id="root",
            invocation_id="inv",
            task_id="task",
            attempt_id="att",
            effect_id="effect-lock",
            kind="test_marker/v1",
            lock_key="effect:effect-lock",
            error_code="progression_lock_timeout",
            now=now + timedelta(milliseconds=1),
            expected=first,
        )
        assert held is not None and held.ordinal == 1
    finally:
        lock.release()
    advanced = store.schedule_next(
        fence_store=fence,
        root_invocation_id="root",
        invocation_id="inv",
        task_id="task",
        attempt_id="att",
        effect_id="effect-lock",
        kind="test_marker/v1",
        lock_key="effect:effect-lock",
        error_code="progression_lock_timeout",
        now=parse_rfc3339_z(first.next_retry_at) + timedelta(seconds=1),
        expected=first,
    )
    assert advanced is not None and advanced.ordinal == 2
