"""commit_archive_outcome: guarded out-of-band archive write (integrity + event)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers_aa import loc_for

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.core.state import (
    read_state,
    verify_state_integrity,
    write_state,
)
from assurance_agent.workflow.orchestration.operations import commit_archive_outcome
from assurance_agent.workflow.orchestration.schema import parse_schema

_SCHEMA = parse_schema(
    """
schema_version: "1"
name: t
params:
  auto_archive: { type: bool, default: false }
phases:
  - id: report
    skill: aa-report
    agent: aa-doc-author
    requires: []
    produces: []
  - id: archive
    skill: aa-archive
    agent: aa-archiver
    requires: [report]
    produces: ["qa/archive/<change-id>/"]
gates: {}
"""
)


def _change(tmp_path: Path) -> Path:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
    return change


def _make_archive_dir(tmp_path: Path) -> None:
    (tmp_path / "qa" / "archive" / "CH-1").mkdir(parents=True, exist_ok=True)


def test_commit_archive_outcome_sets_status_and_rehashes(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _make_archive_dir(tmp_path)

    result = commit_archive_outcome(
        loc_for(change, project_root=tmp_path),
        _SCHEMA,
        status="archived",
        skill="aa-archive",
        skill_md_path="skills/aa-archive/SKILL.md",
    )

    assert result.applied_status == "archived"
    assert result.disposition == "committed"
    # Integrity re-hashed: read_state must not raise and verify passes.
    assert verify_state_integrity(change) is None
    entry = (read_state(change).phases.model_extra or {}).get("archive")
    assert isinstance(entry, dict)
    assert entry["status"] == "archived"
    assert entry["skill"] == "aa-archive"
    # Ledger event emitted for the archive phase.
    committed = [
        e
        for e in read_events(change)
        if e.get("type") == "phase_outcome_committed" and e.get("phase") == "archive"
    ]
    assert len(committed) == 1


def test_commit_archive_outcome_with_warnings(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _make_archive_dir(tmp_path)
    result = commit_archive_outcome(
        loc_for(change, project_root=tmp_path), _SCHEMA, status="archived_with_warnings"
    )
    assert result.applied_status == "archived_with_warnings"
    entry = (read_state(change).phases.model_extra or {}).get("archive")
    assert isinstance(entry, dict)
    assert entry["status"] == "archived_with_warnings"


def test_commit_archive_outcome_rejects_invalid_status(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _make_archive_dir(tmp_path)
    with pytest.raises(AaError, match="invalid archive status"):
        commit_archive_outcome(loc_for(change, project_root=tmp_path), _SCHEMA, status="done")


def test_commit_archive_outcome_requires_produces(tmp_path: Path) -> None:
    change = _change(tmp_path)
    # qa/archive/CH-1 does NOT exist → produces missing for a non-skipped status.
    with pytest.raises(AaError, match="missing declared produces"):
        commit_archive_outcome(loc_for(change, project_root=tmp_path), _SCHEMA, status="archived")


def test_commit_archive_outcome_skipped_allows_missing_produces(tmp_path: Path) -> None:
    change = _change(tmp_path)
    result = commit_archive_outcome(loc_for(change, project_root=tmp_path), _SCHEMA, status="skipped")
    assert result.applied_status == "skipped"


def test_commit_archive_outcome_enforces_skill_attestation(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _make_archive_dir(tmp_path)
    with pytest.raises(AaError, match="SKILL_LOAD_GATE_VIOLATION"):
        commit_archive_outcome(loc_for(change, project_root=tmp_path), _SCHEMA, status="archived", skill=None)


def test_commit_archive_outcome_fails_closed_on_prior_drift(tmp_path: Path) -> None:
    """Guarded commit refuses to write on top of hand-edited (stale-hash) state."""
    change = _change(tmp_path)
    _make_archive_dir(tmp_path)
    commit_archive_outcome(loc_for(change, project_root=tmp_path), _SCHEMA, status="archived")

    # Simulate a hand-edit that appends a field without re-hashing.
    state_file = change / "workflow-state.yaml"
    text = state_file.read_text(encoding="utf-8")
    state_file.write_text(text + "user_requested_archive: true\n", encoding="utf-8")
    assert verify_state_integrity(change) is not None  # drift detected

    # The txn read entry point fails closed on drift → raises rather than
    # silently committing on top of tampered state.
    with pytest.raises(Exception):
        commit_archive_outcome(
            loc_for(change, project_root=tmp_path), _SCHEMA, status="archived_with_warnings"
        )
