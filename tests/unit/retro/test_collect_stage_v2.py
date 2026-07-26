"""Collect stage writes immutable schema-v2 context.json via typed readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assurance_agent.retro.collect_stage import RetroCollectResult, run_retro_collect
from assurance_agent.retro.context import RetroContextImmutableError
from assurance_agent.retro.eval_history import EvalEvidenceSlice, InMemoryEvalHistoryReader
from assurance_agent.retro.types import RetroContext
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.retro.workflow_history import InMemoryWorkflowHistoryReader
from assurance_agent.workflow.issues.history import (
    InMemoryIssueHistoryReader,
    IssueHistoryIntegrityError,
)
from assurance_agent.workflow.issues.history_models import IssueTypedEvents
from tests.helpers_aa import write_aa_config
from tests.unit.retro.test_context import _Readers, _typed_events

pytest_plugins = ["tests.unit.retro.test_context"]


def _empty_eval() -> InMemoryEvalHistoryReader:
    return InMemoryEvalHistoryReader.from_slice(EvalEvidenceSlice())


def test_run_retro_collect_writes_schema_v2_context(tmp_path: Path, readers: _Readers) -> None:
    write_aa_config(tmp_path)
    result = run_retro_collect(
        tmp_path,
        retro_id="retro-v2",
        selection=RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        write_root=tmp_path,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert isinstance(result, RetroCollectResult)
    assert isinstance(result.context, RetroContext)
    assert result.context.schema_version == "2"
    assert result.signal_count == result.context.signal_count
    path = tmp_path / "qa" / "retro" / "retro-v2" / "context.json"
    assert path.is_file()
    loaded = RetroContext.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert loaded.schema_version == "2"
    assert loaded.signal_count == result.signal_count
    # No consumed-state watermark.
    assert not (tmp_path / "qa" / "retro" / "_state.json").exists()


def test_run_retro_collect_idempotent_identical_bytes(tmp_path: Path, readers: _Readers) -> None:
    write_aa_config(tmp_path)
    selection = RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None)
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    first = run_retro_collect(
        tmp_path,
        retro_id="retro-idem",
        selection=selection,
        write_root=tmp_path,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=now,
    )
    second = run_retro_collect(
        tmp_path,
        retro_id="retro-idem",
        selection=selection,
        write_root=tmp_path,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=now,
    )
    path = tmp_path / "qa" / "retro" / "retro-idem" / "context.json"
    assert path.read_bytes() == path.read_bytes()
    assert first.context.model_dump(mode="json") == second.context.model_dump(mode="json")


def test_run_retro_collect_rejects_different_context_bytes(tmp_path: Path, readers: _Readers) -> None:
    write_aa_config(tmp_path)
    run_retro_collect(
        tmp_path,
        retro_id="retro-imm",
        selection=RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        write_root=tmp_path,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    with pytest.raises(RetroContextImmutableError):
        run_retro_collect(
            tmp_path,
            retro_id="retro-imm",
            selection=RetroWindowSelection(change_ids=("RET-1",), last=None),
            write_root=tmp_path,
            issue_history=readers.issues,
            workflow_history=readers.workflow,
            eval_history=readers.eval,
            now=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )


def test_issue_history_integrity_error_hard_fails(tmp_path: Path, readers: _Readers) -> None:
    write_aa_config(tmp_path)
    with pytest.raises(IssueHistoryIntegrityError):
        run_retro_collect(
            tmp_path,
            retro_id="retro-hard",
            selection=RetroWindowSelection(change_ids=("RET-MISSING",), last=None),
            write_root=tmp_path,
            issue_history=readers.issues,
            workflow_history=InMemoryWorkflowHistoryReader(
                terminals=(),
                known_ids=frozenset({"RET-MISSING"}),
            ),
            eval_history=_empty_eval(),
            now=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
    assert not (tmp_path / "qa" / "retro" / "retro-hard" / "context.json").exists()


def test_incomplete_analysis_collects_successfully(tmp_path: Path, readers: _Readers) -> None:
    write_aa_config(tmp_path)
    typed = _typed_events(analysis_failed=True)
    issues = InMemoryIssueHistoryReader.from_events(typed)
    result = run_retro_collect(
        tmp_path,
        retro_id="retro-incomplete",
        selection=RetroWindowSelection(change_ids=("RET-1",), last=None),
        write_root=tmp_path,
        issue_history=issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert result.context.integrity.status == "incomplete"
    assert "analysis_failed" in result.context.integrity.reasons
    assert result.context.allows_domain_knowledge is False
    assert (tmp_path / "qa" / "retro" / "retro-incomplete" / "context.json").is_file()


def test_write_root_receives_context_not_host(tmp_path: Path, readers: _Readers) -> None:
    host = tmp_path / "host"
    workspace = tmp_path / "ws"
    host.mkdir()
    workspace.mkdir()
    write_aa_config(host)
    write_aa_config(workspace)
    result = run_retro_collect(
        host,
        retro_id="retro-split",
        selection=RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        write_root=workspace,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert result.retro_dir == workspace / "qa" / "retro" / "retro-split"
    assert (workspace / "qa" / "retro" / "retro-split" / "context.json").is_file()
    assert not (host / "qa" / "retro").exists()


def test_zero_signal_collect_still_writes_context(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    empty_issues = InMemoryIssueHistoryReader.from_events(
        IssueTypedEvents(
            change_events={},
            problem_events=(),
            change_ledger_bytes={},
            problem_ledger_bytes=b"",
        )
    )
    result = run_retro_collect(
        tmp_path,
        retro_id="retro-zero",
        selection=RetroWindowSelection(last=1),
        write_root=tmp_path,
        issue_history=empty_issues,
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(()),
        eval_history=_empty_eval(),
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert result.signal_count == 0
    assert result.context is not None
    assert result.context.signal_count == 0
