"""Contract tests for EvalHistoryReader adapters (file + in-memory)."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.models.retro_batch import RetroBatchScope
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
    source_change_ids: tuple[str, ...] | None = ("RET-1",),
    schema_version: str | None = "1",
) -> Path:
    run = sut / "qa" / "eval" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    path = run / "report.json"
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
        return path
    payload = {
        "run_id": run_id,
        "suite": suite,
        "verdict": verdict,
        "started_at": started_at,
        "completed_at": started_at,
        "sample_ids": ["S-1"],
        "failure_signature": None if verdict == "pass" else "sha256:failure",
        "raw_report_sha256": "sha256:" + "a" * 64,
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    if source_change_ids is not None:
        payload["source_change_ids"] = list(source_change_ids)
    path.write_text(json.dumps(payload), encoding="utf-8")
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


def test_explicit_change_window_only_reads_linked_projections(tmp_path: Path) -> None:
    _write_report(tmp_path, "run-linked", source_change_ids=("RET-1",))
    _write_report(tmp_path, "run-other", source_change_ids=("RET-2",))
    _write_report(
        tmp_path,
        "run-legacy",
        source_change_ids=None,
        schema_version="2",
    )

    slice_ = FileEvalHistoryReader(tmp_path).read_window(_empty_window())

    assert tuple(report.run_id for report in slice_.reports) == ("run-linked",)
    assert slice_.reports[0].source_change_ids == ("RET-1",)
    assert slice_.integrity.status == "complete"


def test_projection_missing_source_change_ids_is_producer_contract_failure(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "run-invalid-v2",
        source_change_ids=None,
        schema_version="1",
    )

    slice_ = FileEvalHistoryReader(tmp_path).read_window(_empty_window())

    assert slice_.reports == ()
    assert slice_.integrity.status == "incomplete"
    assert slice_.integrity.reasons == ("eval_report_corrupt:run-invalid-v2",)


def test_corrupt_report_outside_window_does_not_mark_incomplete(tmp_path: Path) -> None:
    """Out-of-window dirty runs must not poison integrity for the selected window."""
    _write_report(tmp_path, "run-in", started_at="2026-07-02T00:00:00Z")
    # Valid JSON but schema-corrupt; started_at is resolvable and outside the window.
    stale = tmp_path / "qa" / "eval" / "runs" / "run-stale-bad"
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
    garbage = tmp_path / "qa" / "eval" / "runs" / "run-garbage"
    garbage.mkdir(parents=True)
    (garbage / "report.json").write_text("{not-json", encoding="utf-8")
    # Missing report.json under an old run dir.
    (tmp_path / "qa" / "eval" / "runs" / "run-missing-old").mkdir(parents=True)

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
    bad = tmp_path / "qa" / "eval" / "runs" / "run-bad-in"
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


def test_batch_ignores_raw_eval_and_turns_corrupt_projection_into_member_gap(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "eval/out/runs/raw-only"
    raw.mkdir(parents=True)
    (raw / "report.json").write_text('{"schema_version":"2"}', encoding="utf-8")
    projection = tmp_path / "qa/eval/runs/bad-projection"
    projection.mkdir(parents=True)
    (projection / "report.json").write_text("{not-json", encoding="utf-8")
    scope = RetroBatchScope.model_validate(
        {
            "batch_id": "batch-1",
            "status": "complete",
            "members": [
                {
                    "change_id": "RET-1",
                    "execution_status": "failed",
                    "evidence_availability": "complete",
                }
            ],
        }
    )
    history = InMemoryWorkflowHistoryReader.from_terminals(
        (TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-02T00:00:00Z"),)
    )
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-1",), batch_scope=scope),
        workflow_history=history,
    )

    slice_ = FileEvalHistoryReader(tmp_path).read_window(window)

    assert slice_.reports == ()
    assert slice_.integrity.reasons == ("batch_member_evidence_gap:RET-1:failed:eval:projection_corrupt",)
