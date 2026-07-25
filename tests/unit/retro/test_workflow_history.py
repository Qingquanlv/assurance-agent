"""Contract tests for WorkflowHistoryReader adapters (ledger + in-memory)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

import pytest

from assurance_agent.retro.window import RetroWindowSelection, resolve_retro_window
from assurance_agent.retro.workflow_history import (
    InMemoryWorkflowHistoryReader,
    LedgerWorkflowHistoryReader,
    TerminalChangeRef,
    WorkflowEvidenceSlice,
    WorkflowHistoryIntegrityError,
)
from tests.helpers_aa import write_aa_config

_PUSHBACK = {"needs_fix", "fail", "blocked", "stop"}


def _write_terminal_change(
    project_root: Path,
    change_id: str,
    *,
    terminal_ts: str,
    terminal_type: str = "graph_completed",
    under: str = "archive",
    gate_verdicts: list[dict] | None = None,
    healing_ops: list[str] | None = None,
    phases: dict | None = None,
    apply_applied: bool = True,
) -> Path:
    write_aa_config(project_root)
    root = project_root / "qa" / under / change_id
    root.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, object]] = [
        {
            "seq": 1,
            "ts": "2026-07-01T00:00:00Z",
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": f"inv-{change_id}",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "sha256:" + "a" * 64,
            "contract_digests": {},
            "params": {},
            "params_sha256": "sha256:" + "b" * 64,
            "root_tree_id": "tree-1",
            "max_parallel_tasks": 1,
            "checkpoint_ns": f"inv-{change_id}",
            "structural_path": "/",
        }
    ]
    seq = 2
    for verdict in gate_verdicts or []:
        events.append(
            {
                "seq": seq,
                "ts": terminal_ts,
                "source": "gate",
                "type": "gate_verdict",
                "gate": verdict["gate"],
                "verdict": verdict["verdict"],
                "evidence": verdict.get("evidence", {}),
                **({"reason": verdict["reason"]} if "reason" in verdict else {}),
            }
        )
        seq += 1
    for op_id in healing_ops or []:
        events.append(
            {
                "seq": seq,
                "ts": terminal_ts,
                "source": "progression",
                "type": "healing_attempt_allocated",
                "episode_id": f"ep-{change_id}",
                "attempt_id": f"ha-{change_id}-{seq}",
                "attempt_number": 1,
                "operation_id": op_id,
                "source_batch_id": "b1",
            }
        )
        seq += 1
    events.append(
        {
            "seq": seq,
            "ts": terminal_ts,
            "source": "graph",
            "type": terminal_type,
            "invocation_id": f"inv-{change_id}",
            "checkpoint_ns": f"inv-{change_id}",
            "reason": "settled",
        }
    )
    (root / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    (root / "workflow-state.yaml").write_text(
        yaml.safe_dump({"change_id": change_id, "phases": phases or {}}),
        encoding="utf-8",
    )
    healing = root / "healing"
    healing.mkdir(exist_ok=True)
    (healing / "api-apply-summary.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "target": "api",
                "applied": apply_applied,
                "status": "applied" if apply_applied else "none",
            }
        ),
        encoding="utf-8",
    )
    return root


def test_list_terminal_changes_orders_by_event_ts_not_mtime(tmp_path: Path) -> None:
    # Create RET-1 first (older mtime) but with later terminal_ts.
    early_dir = _write_terminal_change(
        tmp_path, "RET-EARLY-MTIME", terminal_ts="2026-07-03T00:00:00Z"
    )
    time.sleep(0.05)
    late_dir = _write_terminal_change(
        tmp_path, "RET-LATE-MTIME", terminal_ts="2026-07-01T00:00:00Z"
    )
    # Flip directory mtimes so mtime order is opposite of event ts order.
    early_mtime = early_dir.stat().st_mtime
    late_mtime = late_dir.stat().st_mtime
    # Make RET-LATE-MTIME appear newer on disk while its terminal event is older.
    os.utime(late_dir, (early_mtime + 10, early_mtime + 10))
    os.utime(early_dir, (late_mtime - 10, late_mtime - 10))

    reader = LedgerWorkflowHistoryReader(tmp_path)
    terminals = reader.list_terminal_changes()
    ids_by_ts = [t.change_id for t in terminals]
    assert ids_by_ts == ["RET-LATE-MTIME", "RET-EARLY-MTIME"]
    assert terminals[0].terminal_ts == "2026-07-01T00:00:00Z"
    assert terminals[1].terminal_ts == "2026-07-03T00:00:00Z"


def test_last_n_from_ledger_ignores_directory_mtime(tmp_path: Path) -> None:
    _write_terminal_change(tmp_path, "RET-1", terminal_ts="2026-07-01T00:00:00Z")
    time.sleep(0.02)
    _write_terminal_change(tmp_path, "RET-2", terminal_ts="2026-07-02T00:00:00Z")
    time.sleep(0.02)
    _write_terminal_change(tmp_path, "RET-3", terminal_ts="2026-07-03T00:00:00Z")
    # Make RET-1 newest on disk so mtime-based last-N would wrongly pick it.
    time.sleep(0.02)
    (tmp_path / "qa" / "archive" / "RET-1").touch()

    reader = LedgerWorkflowHistoryReader(tmp_path)
    resolved = resolve_retro_window(RetroWindowSelection(last=2), workflow_history=reader)
    assert resolved.change_ids == ("RET-2", "RET-3")


def test_read_window_extracts_gate_healing_and_skill_drift(tmp_path: Path) -> None:
    _write_terminal_change(
        tmp_path,
        "RET-1",
        terminal_ts="2026-07-02T00:00:00Z",
        gate_verdicts=[
            {"gate": "case-review", "verdict": "needs_fix", "reason": "findings open"},
            {"gate": "case-review", "verdict": "pass"},
        ],
        healing_ops=["op-1"],
        phases={
            "explore": {"status": "done", "skill_loaded": False},
            "inspect": {"status": "done", "skill_loaded": True},
        },
        apply_applied=True,
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-1",), last=None),
        workflow_history=reader,
    )
    file_slice = reader.read_window(window)
    memory = InMemoryWorkflowHistoryReader.from_slice(file_slice)
    memory_slice = memory.read_window(window)

    assert isinstance(file_slice, WorkflowEvidenceSlice)
    assert file_slice.model_dump(mode="json") == memory_slice.model_dump(mode="json")
    assert len(file_slice.gate_verdicts) == 1
    assert file_slice.gate_verdicts[0].verdict in _PUSHBACK
    assert file_slice.gate_verdicts[0].evidence_id == "RET-1#seq2"
    assert len(file_slice.healing_allocations) == 1
    assert file_slice.healing_allocations[0].operation_id == "op-1"
    assert file_slice.healing_applies[0].applied is True
    assert len(file_slice.skill_loaded_false) == 1
    assert file_slice.skill_loaded_false[0].phase == "explore"
    assert file_slice.skill_loaded_false[0].evidence_id == "RET-1#workflow-state:explore"
    assert file_slice.integrity.status == "complete"
    assert all(source.kind == "workflow_ledger" for source in file_slice.sources)
    assert file_slice.sources[0].sha256.startswith("sha256:")


def test_missing_change_ledger_marks_incomplete(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    reader = LedgerWorkflowHistoryReader(tmp_path)
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-GONE",), last=None),
        workflow_history=reader,
    )
    slice_ = reader.read_window(window)
    assert slice_.integrity.status == "incomplete"
    assert any("RET-GONE" in reason for reason in slice_.integrity.reasons)
    assert slice_.gate_verdicts == ()


def test_in_memory_list_terminal_changes_sorted() -> None:
    reader = InMemoryWorkflowHistoryReader.from_terminals(
        (
            TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-02T00:00:00Z"),
            TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-01T00:00:00Z"),
        )
    )
    assert [t.change_id for t in reader.list_terminal_changes()] == ["RET-1", "RET-2"]


def test_malformed_ledger_jsonl_raises_integrity_error(tmp_path: Path) -> None:
    """Corrupt workflow ledger must hard-fail; never soft-skip bad lines as complete."""
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "archive" / "RET-BAD"
    change_dir.mkdir(parents=True)
    (change_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "ts": "2026-07-01T00:00:00Z",
                "source": "graph",
                "type": "graph_invocation_started",
                "invocation_id": "inv-RET-BAD",
                "entrypoint": "full",
                "graph_id": "main",
                "graph_digest": "sha256:" + "a" * 64,
                "contract_digests": {},
                "params": {},
                "params_sha256": "sha256:" + "b" * 64,
                "root_tree_id": "tree-1",
                "max_parallel_tasks": 1,
                "checkpoint_ns": "inv-RET-BAD",
                "structural_path": "/",
            }
        )
        + "\n{not-json\n",
        encoding="utf-8",
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    with pytest.raises(WorkflowHistoryIntegrityError, match="RET-BAD"):
        reader.list_terminal_changes()
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-BAD",), last=None),
        workflow_history=InMemoryWorkflowHistoryReader.from_terminals(
            (TerminalChangeRef(change_id="RET-BAD", terminal_ts="2026-07-01T00:00:00Z"),)
        ),
    )
    with pytest.raises(WorkflowHistoryIntegrityError, match="RET-BAD"):
        reader.read_window(window)


def test_non_utf8_ledger_raises_integrity_error(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "archive" / "RET-BIN"
    change_dir.mkdir(parents=True)
    (change_dir / "events.jsonl").write_bytes(b"\xff\xfe not utf-8\n")
    reader = LedgerWorkflowHistoryReader(tmp_path)
    with pytest.raises(WorkflowHistoryIntegrityError, match="RET-BIN"):
        reader.list_terminal_changes()
