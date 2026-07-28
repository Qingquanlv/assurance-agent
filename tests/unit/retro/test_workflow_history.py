"""Contract tests for WorkflowHistoryReader adapters (ledger + in-memory)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

import pytest

from assurance_agent.artifacts.models.retro_batch import RetroBatchScope
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
    task_failure: dict[str, str] | None = None,
    recovery_succeeds: bool | None = None,
    later_recovery_node_succeeds: bool = False,
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
    if task_failure is not None:
        task_id = f"task-{change_id}"
        attempt_id = f"{task_id}-a1"
        events.extend(
            [
                {
                    "seq": seq,
                    "ts": terminal_ts,
                    "source": "graph",
                    "type": "task_attempt_started",
                    "invocation_id": f"inv-{change_id}",
                    "checkpoint_ns": f"inv-{change_id}",
                    "superstep_id": f"step-{change_id}",
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "node_id": task_failure["node_id"],
                    "input_sha256": "sha256:" + "c" * 64,
                    "graph_digest": "sha256:" + "a" * 64,
                    "contract_digest": "sha256:" + "d" * 64,
                    "attempt_number": 1,
                    "lease_expires_at": terminal_ts,
                    "started_at": terminal_ts,
                },
                {
                    "seq": seq + 1,
                    "ts": terminal_ts,
                    "source": "graph",
                    "type": "task_attempt_failed",
                    "invocation_id": f"inv-{change_id}",
                    "checkpoint_ns": f"inv-{change_id}",
                    "superstep_id": f"step-{change_id}",
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "error_kind": task_failure["error_kind"],
                    "message": task_failure["message"],
                    "next_retry_at": None,
                },
            ]
        )
        seq += 2
        if recovery_succeeds is not None:
            recovery_task_id = f"recovery-{change_id}"
            recovery_attempt_id = f"{recovery_task_id}-a1"
            events.extend(
                [
                    {
                        "seq": seq,
                        "ts": terminal_ts,
                        "source": "graph",
                        "type": "task_recovery_routed",
                        "invocation_id": f"inv-{change_id}",
                        "checkpoint_ns": f"inv-{change_id}",
                        "graph_id": "main",
                        "node_id": task_failure["node_id"],
                        "generation_ordinal": 0,
                        "task_id": task_id,
                        "error_kind": task_failure["error_kind"],
                        "message": task_failure["message"],
                        "via": "record-project-sync-pending",
                        "continue_to": "END",
                    },
                    {
                        "seq": seq + 1,
                        "ts": terminal_ts,
                        "source": "graph",
                        "type": "task_attempt_started",
                        "invocation_id": f"inv-{change_id}",
                        "checkpoint_ns": f"inv-{change_id}",
                        "superstep_id": f"recovery-step-{change_id}",
                        "task_id": recovery_task_id,
                        "attempt_id": recovery_attempt_id,
                        "node_id": "record-project-sync-pending",
                        "input_sha256": "sha256:" + "e" * 64,
                        "graph_digest": "sha256:" + "a" * 64,
                        "contract_digest": "sha256:" + "f" * 64,
                        "attempt_number": 1,
                        "lease_expires_at": terminal_ts,
                        "started_at": terminal_ts,
                    },
                    {
                        "seq": seq + 2,
                        "ts": terminal_ts,
                        "source": "graph",
                        "type": ("task_attempt_succeeded" if recovery_succeeds else "task_attempt_failed"),
                        "invocation_id": f"inv-{change_id}",
                        "checkpoint_ns": f"inv-{change_id}",
                        "superstep_id": f"recovery-step-{change_id}",
                        "task_id": recovery_task_id,
                        "attempt_id": recovery_attempt_id,
                        **(
                            {}
                            if recovery_succeeds
                            else {
                                "error_kind": "internal",
                                "message": "recovery handler failed",
                                "next_retry_at": None,
                            }
                        ),
                    },
                ]
            )
            seq += 3
            if later_recovery_node_succeeds:
                later_task_id = f"later-{change_id}"
                later_attempt_id = f"{later_task_id}-a1"
                events.extend(
                    [
                        {
                            "seq": seq,
                            "ts": terminal_ts,
                            "source": "graph",
                            "type": "task_attempt_started",
                            "invocation_id": f"inv-{change_id}",
                            "checkpoint_ns": f"inv-{change_id}",
                            "superstep_id": f"later-step-{change_id}",
                            "task_id": later_task_id,
                            "attempt_id": later_attempt_id,
                            "node_id": "record-project-sync-pending",
                            "input_sha256": "sha256:" + "1" * 64,
                            "graph_digest": "sha256:" + "a" * 64,
                            "contract_digest": "sha256:" + "2" * 64,
                            "attempt_number": 1,
                            "lease_expires_at": terminal_ts,
                            "started_at": terminal_ts,
                        },
                        {
                            "seq": seq + 1,
                            "ts": terminal_ts,
                            "source": "graph",
                            "type": "task_attempt_succeeded",
                            "invocation_id": f"inv-{change_id}",
                            "checkpoint_ns": f"inv-{change_id}",
                            "superstep_id": f"later-step-{change_id}",
                            "task_id": later_task_id,
                            "attempt_id": later_attempt_id,
                        },
                    ]
                )
                seq += 2
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
    (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
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
    early_dir = _write_terminal_change(tmp_path, "RET-EARLY-MTIME", terminal_ts="2026-07-03T00:00:00Z")
    time.sleep(0.05)
    late_dir = _write_terminal_change(tmp_path, "RET-LATE-MTIME", terminal_ts="2026-07-01T00:00:00Z")
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


def test_list_terminal_changes_ignores_completed_nested_graph_without_root_terminal(
    tmp_path: Path,
) -> None:
    """A completed bootstrap child must not make an interrupted Full run terminal."""
    write_aa_config(tmp_path)
    change_id = "RET-INCOMPLETE"
    change_dir = tmp_path / "qa" / "changes" / change_id
    change_dir.mkdir(parents=True)
    events = [
        {
            "seq": 1,
            "ts": "2026-07-01T00:00:00Z",
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "root-full",
            "entrypoint": "full",
            "graph_id": "workflow",
            "graph_digest": "sha256:" + "a" * 64,
            "contract_digests": {},
            "params": {},
            "params_sha256": "sha256:" + "b" * 64,
            "root_tree_id": "tree-root",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "root-full",
            "structural_path": "workflow",
        },
        {
            "seq": 2,
            "ts": "2026-07-01T00:00:01Z",
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "child-bootstrap",
            "entrypoint": "bootstrap",
            "graph_id": "bootstrap",
            "graph_digest": "sha256:" + "a" * 64,
            "contract_digests": {},
            "params": {},
            "params_sha256": "sha256:" + "b" * 64,
            "root_tree_id": "tree-child",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "root-full/bootstrap/child-bootstrap",
            "parent_invocation_id": "root-full",
            "parent_task_id": "task-bootstrap",
            "structural_path": "workflow/bootstrap/bootstrap",
        },
        {
            "seq": 3,
            "ts": "2026-07-01T00:00:02Z",
            "source": "graph",
            "type": "graph_completed",
            "invocation_id": "child-bootstrap",
            "checkpoint_ns": "root-full/bootstrap/child-bootstrap",
            "reason": "END reached",
        },
    ]
    (change_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    assert LedgerWorkflowHistoryReader(tmp_path).list_terminal_changes() == ()


def test_list_terminal_changes_uses_root_full_terminal_not_later_maintenance_run(
    tmp_path: Path,
) -> None:
    """Standalone Archive/Retro terminals must not reorder a completed Change."""
    change_dir = _write_terminal_change(
        tmp_path,
        "RET-COMPLETE",
        terminal_ts="2026-07-01T00:00:10Z",
        under="changes",
    )
    events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().splitlines()]
    events.extend(
        [
            {
                "seq": 3,
                "ts": "2026-07-01T00:01:00Z",
                "source": "graph",
                "type": "graph_invocation_started",
                "invocation_id": "archive-root",
                "entrypoint": "archive",
                "graph_id": "archive-workflow",
                "graph_digest": "sha256:" + "a" * 64,
                "contract_digests": {},
                "params": {},
                "params_sha256": "sha256:" + "b" * 64,
                "root_tree_id": "tree-archive",
                "max_parallel_tasks": 1,
                "checkpoint_ns": "archive-root",
                "structural_path": "archive-workflow",
            },
            {
                "seq": 4,
                "ts": "2026-07-01T00:01:01Z",
                "source": "graph",
                "type": "graph_stopped",
                "invocation_id": "archive-root",
                "checkpoint_ns": "archive-root",
                "reason": "archive gate stop",
            },
            {
                "seq": 5,
                "ts": "2026-07-01T00:02:00Z",
                "source": "graph",
                "type": "graph_invocation_started",
                "invocation_id": "retro-root",
                "entrypoint": "retro",
                "graph_id": "retro-workflow",
                "graph_digest": "sha256:" + "a" * 64,
                "contract_digests": {},
                "params": {},
                "params_sha256": "sha256:" + "b" * 64,
                "root_tree_id": "tree-retro",
                "max_parallel_tasks": 1,
                "checkpoint_ns": "retro-root",
                "structural_path": "retro-workflow",
            },
            {
                "seq": 6,
                "ts": "2026-07-01T00:02:01Z",
                "source": "graph",
                "type": "graph_completed",
                "invocation_id": "retro-root",
                "checkpoint_ns": "retro-root",
                "reason": "END reached",
            },
        ]
    )
    (change_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    terminal = LedgerWorkflowHistoryReader(tmp_path).list_terminal_changes()[0]
    assert terminal.terminal_ts == "2026-07-01T00:00:10Z"
    assert terminal.terminal_event_type == "graph_completed"


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
            "explore": {
                "status": "done",
                "skill_loaded": False,
                "skill_md_path": "skills/aa-explore/SKILL.md",
            },
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
    assert file_slice.skill_loaded_false[0].expected_skill == "aa-explore"
    assert file_slice.skill_loaded_false[0].evidence_id == "RET-1#workflow-state:explore"
    assert file_slice.integrity.status == "complete"
    assert all(source.kind == "workflow_ledger" for source in file_slice.sources)
    assert file_slice.sources[0].sha256.startswith("sha256:")


def test_read_window_extracts_graph_runtime_nested_gate_report(tmp_path: Path) -> None:
    change_dir = _write_terminal_change(
        tmp_path,
        "RET-GATE-REPORT",
        terminal_ts="2026-07-02T00:00:00Z",
    )
    events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().splitlines()]
    terminal = events.pop()
    events.extend(
        [
            {
                "seq": 2,
                "ts": "2026-07-01T23:59:58Z",
                "source": "graph",
                "type": "task_attempt_started",
                "invocation_id": "inv-RET-GATE-REPORT",
                "checkpoint_ns": "inv-RET-GATE-REPORT",
                "superstep_id": "step-gate",
                "task_id": "task-gate",
                "attempt_id": "task-gate-a1",
                "node_id": "precheck",
                "input_sha256": "sha256:" + "c" * 64,
                "graph_digest": "sha256:" + "a" * 64,
                "contract_digest": "sha256:" + "d" * 64,
                "attempt_number": 1,
                "lease_expires_at": "2026-07-02T00:01:00Z",
                "started_at": "2026-07-01T23:59:58Z",
            },
            {
                "seq": 3,
                "ts": "2026-07-01T23:59:59Z",
                "source": "graph",
                "type": "task_attempt_succeeded",
                "invocation_id": "inv-RET-GATE-REPORT",
                "checkpoint_ns": "inv-RET-GATE-REPORT",
                "superstep_id": "step-gate",
                "task_id": "task-gate",
                "attempt_id": "task-gate-a1",
                "gate_report": {
                    "gate_id": "archive-gate",
                    "verdict": "stop",
                    "reason": "execution final_status is FAIL",
                    "matched_rule": "stop_when",
                    "details": {"cause": "archive.execution_failed"},
                    "reads_sha256": {},
                    "value": "stop",
                },
            },
            {**terminal, "seq": 4},
        ]
    )
    (change_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-GATE-REPORT",), last=None),
        workflow_history=reader,
    )

    slice_ = reader.read_window(window)

    assert len(slice_.gate_verdicts) == 1
    assert slice_.gate_verdicts[0].gate == "archive-gate"
    assert slice_.gate_verdicts[0].verdict == "stop"
    assert slice_.gate_verdicts[0].cause == "archive.execution_failed"
    assert slice_.gate_verdicts[0].evidence_id == "RET-GATE-REPORT#seq3"


def test_unrecovered_issue_subgraph_failure_is_evidence_and_marks_incomplete(
    tmp_path: Path,
) -> None:
    _write_terminal_change(
        tmp_path,
        "RET-ISSUE-FAIL",
        terminal_ts="2026-07-02T00:00:00Z",
        terminal_type="graph_failed",
        task_failure={
            "node_id": "inspect-with-issues",
            "error_kind": "conflict",
            "message": "timed out acquiring project:issue-registry",
        },
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-ISSUE-FAIL",), last=None),
        workflow_history=reader,
    )

    slice_ = reader.read_window(window)

    assert len(slice_.task_failures) == 1
    failure = slice_.task_failures[0]
    assert failure.node_id == "inspect-with-issues"
    assert failure.task_id == "task-RET-ISSUE-FAIL"
    assert failure.attempt_id == "task-RET-ISSUE-FAIL-a1"
    assert failure.ts == "2026-07-02T00:00:00Z"
    assert failure.error_kind == "conflict"
    assert failure.recovered is False
    assert failure.evidence_id == "RET-ISSUE-FAIL#seq3"
    assert failure.evidence_id in slice_.sources[0].evidence_ids
    assert slice_.integrity.status == "incomplete"
    assert slice_.integrity.reasons == ("issue_pipeline_failed:RET-ISSUE-FAIL",)


@pytest.mark.parametrize(
    ("recovery_succeeds", "expected_recovered", "expected_integrity"),
    [(False, False, "incomplete"), (True, True, "complete")],
)
def test_recovery_is_confirmed_by_recovery_task_settlement(
    tmp_path: Path,
    recovery_succeeds: bool,
    expected_recovered: bool,
    expected_integrity: str,
) -> None:
    _write_terminal_change(
        tmp_path,
        "RET-ISSUE-RECOVERY",
        terminal_ts="2026-07-26T02:00:00Z",
        terminal_type="graph_completed" if recovery_succeeds else "graph_failed",
        task_failure={
            "node_id": "inspect-with-issues",
            "error_kind": "conflict",
            "message": "timed out acquiring project:issue-registry",
        },
        recovery_succeeds=recovery_succeeds,
        later_recovery_node_succeeds=not recovery_succeeds,
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    window = resolve_retro_window(
        RetroWindowSelection(change_ids=("RET-ISSUE-RECOVERY",), last=None),
        workflow_history=reader,
    )

    slice_ = reader.read_window(window)

    issue_failure = next(
        failure for failure in slice_.task_failures if failure.node_id == "inspect-with-issues"
    )
    assert issue_failure.recovered is expected_recovered
    assert slice_.integrity.status == expected_integrity


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


def _batch_scope(*members: tuple[str, str, str]) -> RetroBatchScope:
    return RetroBatchScope.model_validate(
        {
            "batch_id": "batch-1",
            "status": "complete" if all(member[2] == "complete" for member in members) else "incomplete",
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


def test_batch_running_member_does_not_read_ledger_and_valid_member_survives(
    tmp_path: Path,
) -> None:
    _write_terminal_change(tmp_path, "CH-COMPLETE", terminal_ts="2026-07-01T00:00:00Z")
    running = tmp_path / "qa" / "changes" / "CH-RUNNING"
    running.mkdir(parents=True)
    (running / "events.jsonl").write_text("not-json\n", encoding="utf-8")
    scope = _batch_scope(
        ("CH-COMPLETE", "completed", "complete"),
        ("CH-RUNNING", "running", "partial"),
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("CH-COMPLETE", "CH-RUNNING"), batch_scope=scope),
        workflow_history=reader,
    )

    slice_ = reader.read_window(resolved)

    assert tuple(source.change_id for source in slice_.sources) == ("CH-COMPLETE",)
    assert "batch_member_evidence_gap:CH-RUNNING:running:workflow:non_terminal" in slice_.integrity.reasons


def test_batch_corrupt_member_does_not_discard_complete_member(tmp_path: Path) -> None:
    _write_terminal_change(tmp_path, "CH-COMPLETE", terminal_ts="2026-07-01T00:00:00Z")
    corrupt = tmp_path / "qa" / "archive" / "CH-CORRUPT"
    corrupt.mkdir(parents=True)
    (corrupt / "events.jsonl").write_text("not-json\n", encoding="utf-8")
    scope = _batch_scope(
        ("CH-COMPLETE", "completed", "complete"),
        ("CH-CORRUPT", "failed", "complete"),
    )
    reader = LedgerWorkflowHistoryReader(tmp_path)
    resolved = resolve_retro_window(
        RetroWindowSelection(change_ids=("CH-COMPLETE", "CH-CORRUPT"), batch_scope=scope),
        workflow_history=reader,
    )

    slice_ = reader.read_window(resolved)

    assert tuple(source.change_id for source in slice_.sources) == ("CH-COMPLETE",)
    assert any(reason.endswith(":workflow:ledger_corrupt") for reason in slice_.integrity.reasons)


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
