"""Deterministic Retro window selection: mutually exclusive modes and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.retro.window import (
    ResolvedRetroWindow,
    RetroWindowSelection,
    resolve_retro_window,
)
from assurance_agent.retro.workflow_history import (
    InMemoryWorkflowHistoryReader,
    TerminalChangeRef,
)


def test_window_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="exactly one window mode"):
        RetroWindowSelection(change_ids=("RET-1",), since="2026-07-01T00:00:00Z", last=None)


def test_default_construction_uses_last_mode() -> None:
    selection = RetroWindowSelection()
    assert selection.last == 10
    assert selection.change_ids == ()
    assert selection.since is None


def test_explicit_ids_require_last_none() -> None:
    with pytest.raises(ValueError, match="exactly one window mode"):
        RetroWindowSelection(change_ids=("RET-1",))


def test_explicit_ids_sorted_and_deduplicated() -> None:
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (
            TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-02T00:00:00Z"),
            TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-01T00:00:00Z"),
            TerminalChangeRef(change_id="RET-3", terminal_ts="2026-07-03T00:00:00Z"),
        )
    )
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-3", "RET-1", "RET-3", "RET-2"), last=None),
        workflow_history=history,
    )
    assert resolved.change_ids == ("RET-1", "RET-2", "RET-3")
    assert resolved.selection.mode == "change_ids"


def test_last_n_uses_terminal_event_time_not_directory_mtime(history) -> None:
    resolved = resolve_retro_window(RetroWindowSelection(last=2), workflow_history=history)
    assert resolved.change_ids == ("RET-2", "RET-3")


@pytest.fixture
def history() -> InMemoryWorkflowHistoryReader:
    """Three terminal Changes ordered by authoritative event ts (not insertion order)."""
    return InMemoryWorkflowHistoryReader.from_terminals(
        (
            TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-01T00:00:00Z"),
            TerminalChangeRef(change_id="RET-3", terminal_ts="2026-07-03T00:00:00Z"),
            TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-02T00:00:00Z"),
        )
    )


def test_time_range_closed_boundaries(history: InMemoryWorkflowHistoryReader) -> None:
    resolved = resolve_retro_window(
        RetroWindowSelection(since="2026-07-02T00:00:00Z", until="2026-07-02T00:00:00Z", last=None),
        workflow_history=history,
    )
    assert resolved.change_ids == ("RET-2",)
    assert resolved.since == "2026-07-02T00:00:00Z"
    assert resolved.until == "2026-07-02T00:00:00Z"


def test_omitted_until_pins_source_head(history: InMemoryWorkflowHistoryReader) -> None:
    resolved = resolve_retro_window(
        RetroWindowSelection(since="2026-07-01T00:00:00Z", last=None),
        workflow_history=history,
    )
    assert resolved.change_ids == ("RET-1", "RET-2", "RET-3")
    assert resolved.until == "2026-07-03T00:00:00Z"


def test_stable_tie_break_by_change_id() -> None:
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (
            TerminalChangeRef(change_id="RET-B", terminal_ts="2026-07-01T00:00:00Z"),
            TerminalChangeRef(change_id="RET-A", terminal_ts="2026-07-01T00:00:00Z"),
            TerminalChangeRef(change_id="RET-C", terminal_ts="2026-07-02T00:00:00Z"),
        )
    )
    resolved = resolve_retro_window(RetroWindowSelection(last=2), workflow_history=history)
    # Same ts: RET-A before RET-B; last 2 by (ts, id) → RET-B then RET-C; sorted output.
    assert resolved.change_ids == ("RET-B", "RET-C")


def test_missing_workflow_source_is_degraded_reason() -> None:
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-01T00:00:00Z"),)
    )
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-1", "RET-MISSING"), last=None),
        workflow_history=history,
    )
    assert resolved.change_ids == ("RET-1", "RET-MISSING")
    assert resolved.integrity.status == "incomplete"
    assert any("RET-MISSING" in reason for reason in resolved.integrity.reasons)


def test_resolve_does_not_mark_consumed(tmp_path: Path, history: InMemoryWorkflowHistoryReader) -> None:
    state_path = tmp_path / "qa" / "retro" / "_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"last_retro_id": null, "consumed_changes": {}}\n', encoding="utf-8")
    before = state_path.read_bytes()

    resolved = resolve_retro_window(RetroWindowSelection(last=1), workflow_history=history)
    assert isinstance(resolved, ResolvedRetroWindow)
    assert state_path.read_bytes() == before
    assert not (tmp_path / "qa" / "retro" / "_state.json.bak").exists()


def test_to_issue_selection_and_context_window(history: InMemoryWorkflowHistoryReader) -> None:
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-2", "RET-1"), last=None),
        workflow_history=history,
    )
    issue_sel = resolved.to_issue_selection()
    assert issue_sel.change_ids == ("RET-1", "RET-2")
    context_window = resolved.to_context_window()
    assert context_window.change_ids == ("RET-1", "RET-2")
    assert context_window.selection.mode == "change_ids"
