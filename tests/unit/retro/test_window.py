"""Deterministic Retro window selection: mutually exclusive modes and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.retro_batch import RetroBatchScope
from assurance_agent.retro.window import (
    BatchScopeContractError,
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


def test_default_construction_does_not_manufacture_last_mode() -> None:
    with pytest.raises(ValidationError, match="exactly one window mode"):
        RetroWindowSelection()


def test_explicit_ids_do_not_require_a_last_override() -> None:
    selection = RetroWindowSelection(change_ids=("RET-1",))
    assert selection.last is None


def _batch_scope(members: tuple[tuple[str, str, str], ...], *, status: str = "incomplete") -> RetroBatchScope:
    return RetroBatchScope.model_validate(
        {
            "batch_id": "batch-20260728",
            "status": status,
            "members": [
                {
                    "change_id": change_id,
                    "execution_status": execution_status,
                    "evidence_availability": availability,
                }
                for change_id, execution_status, availability in members
            ],
        }
    )


def test_batch_requires_exact_member_identity_and_order() -> None:
    scope = _batch_scope(
        (
            ("RET-1", "completed", "complete"),
            ("RET-2", "failed", "complete"),
        ),
        status="complete",
    )
    with pytest.raises(BatchScopeContractError) as exc_info:
        RetroWindowSelection(change_ids=("RET-2", "RET-1"), last=None, batch_scope=scope)
    assert exc_info.value.error_kind == "batch_scope_invalid"
    assert exc_info.value.reason_code == "member_set_or_order_mismatch"


def test_batch_is_mutually_exclusive_with_non_explicit_modes() -> None:
    scope = _batch_scope((("RET-1", "completed", "complete"),), status="complete")
    with pytest.raises(BatchScopeContractError) as exc_info:
        RetroWindowSelection(change_ids=("RET-1",), last=1, batch_scope=scope)
    assert exc_info.value.reason_code == "window_mode_conflict"


def test_batch_resolution_retains_all_members_and_only_downgrades_availability() -> None:
    member_rows = tuple((f"RET-{index:02d}", "completed", "complete") for index in range(1, 12))
    scope = _batch_scope(member_rows, status="complete")
    history = InMemoryWorkflowHistoryReader(
        terminals=(TerminalChangeRef(change_id="RET-01", terminal_ts="2026-07-01T00:00:00Z"),),
        known_ids=frozenset({"RET-01", "RET-02", "RET-OLD"}),
    )

    resolved = resolve_retro_window(
        RetroWindowSelection(
            change_ids=tuple(row[0] for row in member_rows),
            last=None,
            batch_scope=scope,
        ),
        workflow_history=history,
    )

    assert resolved.change_ids == tuple(row[0] for row in member_rows)
    assert "RET-OLD" not in resolved.change_ids
    assert resolved.selection.mode == "change_ids"
    assert resolved.batch_scope is not None
    availability = {member.change_id: member.evidence_availability for member in resolved.batch_scope.members}
    assert availability["RET-01"] == "complete"
    assert availability["RET-02"] == "partial"
    assert availability["RET-03"] == "absent"
    assert resolved.batch_scope.status == "incomplete"
    assert resolved.to_context_window().batch_scope == resolved.batch_scope


def test_batch_resolution_never_upgrades_manifest_availability() -> None:
    scope = _batch_scope((("RET-1", "completed", "partial"),))
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-01T00:00:00Z"),)
    )
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-1",), last=None, batch_scope=scope),
        workflow_history=history,
    )
    assert resolved.batch_scope is not None
    assert resolved.batch_scope.members[0].evidence_availability == "partial"


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
