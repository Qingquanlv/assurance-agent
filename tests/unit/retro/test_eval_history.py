"""Contract tests for EvalHistoryReader adapters (file + in-memory)."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.eval_history import (
    EvalEvidenceSlice,
    FileEvalHistoryReader,
    InMemoryEvalHistoryReader,
)
from assurance_agent.retro.window import (
    ResolvedRetroWindow,
    RetroWindowSelection,
    resolve_retro_window,
)
from assurance_agent.retro.workflow_history import InMemoryWorkflowHistoryReader, TerminalChangeRef


def _write_report(
    sut: Path,
    run_id: str,
    *,
    suite: str = "workflow-case",
    verdict: str = "pass",
    started_at: str = "2026-07-02T00:00:00Z",
    corrupt: bool = False,
) -> Path:
    run = sut / "eval" / "out" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    path = run / "report.json"
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
        return path
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "suite": suite,
                "verdict": verdict,
                "started_at": started_at,
                "completed_at": started_at,
                "sample_ids": ["S-1"],
                "sample_count": 1,
                "metrics": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _empty_window() -> ResolvedRetroWindow:
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-02T12:00:00Z"),)
    )
    return resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-1",), last=None),
        workflow_history=history,
    )


def test_file_and_memory_adapters_return_same_slice(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-1", started_at="2026-07-01T00:00:00Z")
    _write_report(tmp_path, "run-2", started_at="2026-07-03T00:00:00Z", verdict="fail")
    window = resolve_retro_window(
        RetroWindowSelection(since="2026-07-01T00:00:00Z", until="2026-07-03T00:00:00Z", last=None),
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(
            (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-02T00:00:00Z"),)
        ),
    )
    file_slice = FileEvalHistoryReader(tmp_path).read_window(window)
    memory_slice = InMemoryEvalHistoryReader.from_slice(file_slice).read_window(window)
    assert file_slice.model_dump(mode="json") == memory_slice.model_dump(mode="json")
    assert isinstance(file_slice, EvalEvidenceSlice)
    assert tuple(r.run_id for r in file_slice.reports) == ("run-1", "run-2")
    assert all(source.kind == "eval_run" for source in file_slice.sources)
    assert all(source.head_event_id == source.evidence_ids[0] for source in file_slice.sources)
    assert file_slice.integrity.status == "complete"


def test_time_range_filters_started_at_closed(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-before", started_at="2026-06-30T00:00:00Z")
    _write_report(tmp_path, "run-in", started_at="2026-07-01T00:00:00Z")
    _write_report(tmp_path, "run-after", started_at="2026-07-05T00:00:00Z")
    window = resolve_retro_window(
        RetroWindowSelection(since="2026-07-01T00:00:00Z", until="2026-07-01T00:00:00Z", last=None),
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(()),
    )
    # Empty terminals still allow time-range window with pinned bounds.
    assert window.since == "2026-07-01T00:00:00Z"
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert tuple(r.run_id for r in slice_.reports) == ("run-in",)


def test_missing_eval_runs_is_incomplete_not_fake_complete(tmp_path: Path) -> None:
    window = _empty_window()
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert slice_.reports == ()
    assert slice_.integrity.status == "incomplete"
    assert slice_.integrity.reasons
    assert any("eval" in reason for reason in slice_.integrity.reasons)


def test_corrupt_report_records_concrete_reason(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-ok", started_at="2026-07-02T00:00:00Z")
    _write_report(tmp_path, "run-bad", corrupt=True)
    window = _empty_window()
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert tuple(r.run_id for r in slice_.reports) == ("run-ok",)
    assert slice_.integrity.status == "incomplete"
    assert any("run-bad" in reason for reason in slice_.integrity.reasons)


def test_run_id_is_source_reference(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-ref", started_at="2026-07-02T00:00:00Z")
    window = _empty_window()
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert slice_.sources[0].evidence_ids == ("run-ref",)
    assert "run-ref" in slice_.resolvable_ids()


def test_corrupt_report_outside_window_does_not_mark_incomplete(tmp_path: Path) -> None:
    """Out-of-window dirty runs must not poison integrity for the selected window."""
    _write_report(tmp_path, "run-in", started_at="2026-07-02T00:00:00Z")
    # Valid JSON but schema-corrupt; started_at is resolvable and outside the window.
    stale = tmp_path / "eval" / "out" / "runs" / "run-stale-bad"
    stale.mkdir(parents=True)
    (stale / "report.json").write_text(
        json.dumps(
            {
                "run_id": "run-stale-bad",
                "started_at": "2020-01-01T00:00:00Z",
                "suite": "workflow-case",
                # missing required verdict → corrupt
            }
        ),
        encoding="utf-8",
    )
    # Completely unreadable garbage also outside any selected window.
    garbage = tmp_path / "eval" / "out" / "runs" / "run-garbage"
    garbage.mkdir(parents=True)
    (garbage / "report.json").write_text("{not-json", encoding="utf-8")
    # Missing report.json under an old run dir.
    (tmp_path / "eval" / "out" / "runs" / "run-missing-old").mkdir(parents=True)

    window = resolve_retro_window(
        RetroWindowSelection(since="2026-07-01T00:00:00Z", until="2026-07-03T00:00:00Z", last=None),
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(
            (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-02T00:00:00Z"),)
        ),
    )
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert tuple(r.run_id for r in slice_.reports) == ("run-in",)
    assert slice_.integrity.status == "complete"
    assert slice_.integrity.reasons == ()


def test_corrupt_report_inside_window_marks_incomplete(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-ok", started_at="2026-07-02T00:00:00Z")
    bad = tmp_path / "eval" / "out" / "runs" / "run-bad-in"
    bad.mkdir(parents=True)
    (bad / "report.json").write_text(
        json.dumps(
            {
                "run_id": "run-bad-in",
                "started_at": "2026-07-02T12:00:00Z",
                "suite": "workflow-case",
            }
        ),
        encoding="utf-8",
    )
    window = resolve_retro_window(
        RetroWindowSelection(since="2026-07-01T00:00:00Z", until="2026-07-03T00:00:00Z", last=None),
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(
            (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-02T00:00:00Z"),)
        ),
    )
    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)
    assert tuple(r.run_id for r in slice_.reports) == ("run-ok",)
    assert slice_.integrity.status == "incomplete"
    assert any("run-bad-in" in reason for reason in slice_.integrity.reasons)
