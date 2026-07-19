"""Ledger 投影与 checkpoint snapshot：strict graph events 是唯一权威。

覆盖：任务生命周期 fold、superstep commit 推进、预算去重、interrupt/resume、
terminal 事件、snapshot 损坏重建、workflow-state.yaml 投影重建与损坏无关性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.core.events import LedgerIntegrityError, append_event_strict
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointStore,
    project_invocation,
    project_workflow_state,
    render_workflow_state_yaml,
)
from assurance_agent.workflow.graph.models import GraphProjection


def _started(inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": inv,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "gd-1",
        "contract_digests": {"skill:noop": "cd-1"},
        "params": {"run_mode": "full"},
        "params_sha256": "ps-1",
        "root_tree_id": "tree-0",
        "max_parallel_tasks": 2,
        "checkpoint_ns": inv,
        "structural_path": "main",
    }


def _planned(task_ids: list[str], inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "superstep_planned",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "checkpoint_id": "cp-0",
        "task_ids": task_ids,
    }


def _begin(task_id: str, node_id: str, attempt: int = 1, inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_started",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "node_id": node_id,
        "input_sha256": "in-1",
        "graph_digest": "gd-1",
        "contract_digest": "cd-1",
        "attempt_number": attempt,
        "lease_expires_at": "2026-07-19T00:00:00+00:00",
        "started_at": "2026-07-19T00:00:00+00:00",
    }


def _succeeded(task_id: str, write_set_id: str, attempt: int = 1, inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_succeeded",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "write_set_id": write_set_id,
        "outputs_sha256": {"out.json": "oh-1"},
        "gate_report": None,
        "state_updates": {"k": "v"},
    }


def _failed(task_id: str, inv: str = "inv-1", next_retry_at: str | None = None) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_failed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a1",
        "error_kind": "internal",
        "message": "boom",
        "next_retry_at": next_retry_at,
    }


def _committed(inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "superstep_committed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "checkpoint_id": "cp-1",
        "parent_checkpoint_id": "cp-0",
        "write_set_ids": ["ws-a"],
        "target_tree_id": "tree-1",
        "state_values": {"k": "v"},
    }


def _budget(budget_id: str, consumption_id: str, task_id: str, inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "budget_consumed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "graph_id": "main",
        "budget_id": budget_id,
        "consumption_id": consumption_id,
        "task_id": task_id,
    }


def _interrupted(interrupt_id: str = "ir-1", inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_interrupted",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "interrupt_id": interrupt_id,
        "node_id": "review",
        "checkpoint": "cp-1",
        "actions": ["approve", "stop"],
        "audited_reads_sha256": {"review/case.json": "rh-1"},
        "artifact_view": None,
    }


def _resumed(interrupt_id: str = "ir-1", action: str = "approve", inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_resumed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "interrupt_id": interrupt_id,
        "action": action,
        "reason": "looks good",
        "who": "reviewer",
        "audited_reads_sha256": {"review/case.json": "rh-1"},
    }


def _terminal(inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_completed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "reason": "all nodes settled",
    }


def _append_all(change: Path, events: list[dict]) -> None:
    for event in events:
        append_event_strict(change, event)


def _lifecycle_events() -> list[dict]:
    """plan 指定的场景：invocation → plan → two starts → one success → one failure。"""
    return [
        _started(),
        _planned(["task-a", "task-b"]),
        _begin("task-a", "node-a"),
        _begin("task-b", "node-b"),
        _succeeded("task-a", "ws-a"),
        _failed("task-b"),
    ]


def test_projection_folds_task_lifecycle(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, _lifecycle_events())

    projection = project_invocation(change, "inv-1")
    assert projection.tasks["task-a"].status == "succeeded"
    assert projection.tasks["task-a"].write_set_id == "ws-a"
    assert projection.tasks["task-b"].status == "failed"
    assert projection.tasks["task-b"].attempts_used == 1
    assert projection.latest_checkpoint_id is None


def test_superstep_commit_projects_checkpoint_and_tree(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [*_lifecycle_events(), _committed()])

    projection = project_invocation(change, "inv-1")
    assert projection.latest_checkpoint_id == "cp-1"
    assert projection.current_tree_id == "tree-1"
    assert projection.root_tree_id == "tree-0"
    assert projection.state_values == {"k": "v"}
    assert projection.event_seq == 7
    assert projection.supersteps == 1
    # task 投影保持 succeeded/failed，不被 commit 改写。
    assert projection.tasks["task-a"].status == "succeeded"
    assert projection.tasks["task-b"].status == "failed"


def test_duplicate_budget_consumption_is_integrity_error(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _planned(["task-a"]),
            _begin("task-a", "node-a"),
            _succeeded("task-a", "ws-a"),
            _budget("max_fix_attempts", "c-1", "task-a"),
        ],
    )
    projection = project_invocation(change, "inv-1")
    assert projection.budgets == {"max_fix_attempts": 1}

    # 同一 (invocation_id, budget_id, consumption_id) 重复：完整性失败，而非重复计数。
    append_event_strict(change, _budget("max_fix_attempts", "c-1", "task-a"))
    with pytest.raises(LedgerIntegrityError, match="budget"):
        project_invocation(change, "inv-1")


def test_retry_attempts_accumulate_separately(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _planned(["task-a"]),
            _begin("task-a", "node-a", attempt=1),
            _failed("task-a", next_retry_at="2026-07-19T00:01:00+00:00"),
            _begin("task-a", "node-a", attempt=2),
        ],
    )
    projection = project_invocation(change, "inv-1")
    task = projection.tasks["task-a"]
    assert task.attempts_used == 2
    assert task.status == "running"
    assert task.latest_attempt_id == "task-a-a2"
    # 新 attempt 清除上一次失败的 retry 游标。
    assert task.error_kind is None
    assert task.next_retry_at is None


def test_task_outcome_without_start_is_integrity_error(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), _succeeded("task-a", "ws-a")])
    with pytest.raises(LedgerIntegrityError, match="unknown task"):
        project_invocation(change, "inv-1")


def test_missing_invocation_start_is_integrity_error(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_planned(["task-a"])])
    with pytest.raises(LedgerIntegrityError, match="graph_invocation_started"):
        project_invocation(change, "inv-1")


def test_interrupt_resume_and_terminal_fold(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [*_lifecycle_events(), _committed(), _interrupted()])

    projection = project_invocation(change, "inv-1")
    interrupt = projection.interrupts["ir-1"]
    assert interrupt.resolved_action is None
    assert interrupt.actions == ("approve", "stop")
    view = project_workflow_state(projection)
    assert [i.interrupt_id for i in view.pending_interrupts] == ["ir-1"]

    _append_all(change, [_resumed(), _terminal()])
    projection = project_invocation(change, "inv-1")
    assert projection.interrupts["ir-1"].resolved_action == "approve"
    assert projection.terminal == "completed"
    assert projection.terminal_reason == "all nodes settled"
    view = project_workflow_state(projection)
    assert view.pending_interrupts == ()
    assert view.terminal == "completed"
    assert view.latest_checkpoint_id == "cp-1"
    assert view.nodes == {"node-a": ("task-a",), "node-b": ("task-b",)}


def test_resume_unknown_interrupt_is_integrity_error(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), _resumed("ir-ghost")])
    with pytest.raises(LedgerIntegrityError, match="unknown interrupt"):
        project_invocation(change, "inv-1")


def test_imported_task_folds_as_succeeded_without_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    imported = {
        "source": "graph",
        "type": "task_imported",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "graph_id": "main",
        "node_id": "node-a",
        "structural_path": "main",
        "task_key": None,
        "outputs_sha256": {"out.json": "oh-1"},
        "gate_report": None,
    }
    _append_all(change, [_started(), imported])
    projection = project_invocation(change, "inv-1")
    task = projection.tasks["main:node-a"]
    assert task.status == "succeeded"
    assert task.attempts_used == 0  # 导入不伪造物理 attempt
    assert task.outputs_sha256 == {"out.json": "oh-1"}


def test_checkpoint_store_rebuilds_corrupt_snapshot(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [*_lifecycle_events(), _committed()])

    store = CheckpointStore(change)
    projection = project_invocation(change, "inv-1")
    path = store.write(projection)
    assert path == change / ".graph-runtime/checkpoints/cp-1.json"
    assert json.loads(path.read_text(encoding="utf-8"))["invocation_id"] == "inv-1"

    # 损坏 snapshot：read_latest 从 ledger 重建并覆写合法缓存。
    path.write_text("{corrupt", encoding="utf-8")
    rebuilt = store.read_latest("inv-1")
    assert rebuilt == projection
    cached = GraphProjection.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert cached == projection


def test_checkpoint_store_keeps_valid_snapshot_and_tracks_ledger(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, _lifecycle_events())

    store = CheckpointStore(change)
    first = store.read_latest("inv-1")
    assert first.latest_checkpoint_id is None
    path = change / ".graph-runtime/checkpoints/bootstrap-inv-1.json"
    assert path.exists()
    before = path.read_bytes()
    # 有效 snapshot：不覆写。
    assert store.read_latest("inv-1") == first
    assert path.read_bytes() == before

    # ledger 前进后 event_seq 落后：重建并覆写新 snapshot。
    append_event_strict(change, _committed())
    advanced = store.read_latest("inv-1")
    assert advanced.latest_checkpoint_id == "cp-1"
    assert advanced.event_seq == first.event_seq + 1


def test_workflow_state_yaml_rebuilds_semantically_identical(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            *_lifecycle_events(),
            _committed(),
            _budget("max_fix_attempts", "c-1", "task-a"),
            _interrupted(),
            _terminal(),
        ],
    )

    projection = project_invocation(change, "inv-1")
    original = render_workflow_state_yaml(projection)
    with transaction(change) as txn:
        txn.set_workflow_state_projection(original)
    state_file = change / "workflow-state.yaml"
    assert yaml.safe_load(state_file.read_text(encoding="utf-8")) == yaml.safe_load(
        original.decode("utf-8")
    )

    # 删除投影文件后从 strict ledger 重建：字节在语义上完全一致。
    state_file.unlink()
    assert not state_file.exists()
    rebuilt = render_workflow_state_yaml(project_invocation(change, "inv-1"))
    with transaction(change) as txn:
        txn.set_workflow_state_projection(rebuilt)
    assert yaml.safe_load(state_file.read_text(encoding="utf-8")) == yaml.safe_load(
        original.decode("utf-8")
    )


def test_corrupt_workflow_state_yaml_does_not_affect_projection(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _planned(["task-a"]),
            _begin("task-a", "node-a", attempt=1),
            _failed("task-a", next_retry_at="2026-07-19T00:01:00+00:00"),
            _begin("task-a", "node-a", attempt=2),
            _succeeded("task-a", "ws-a", attempt=2),
            _committed(),
            _budget("max_fix_attempts", "c-1", "task-a"),
            _interrupted(),
        ],
    )

    before = project_invocation(change, "inv-1")
    with transaction(change) as txn:
        txn.set_workflow_state_projection(render_workflow_state_yaml(before))

    # 损坏 workflow-state.yaml：任务完成、预算、interrupt、retry 计数与投影都不变。
    (change / "workflow-state.yaml").write_bytes(b"\x00\x01{not yaml")
    after = project_invocation(change, "inv-1")
    assert after == before
    assert after.tasks["task-a"].status == "succeeded"
    assert after.tasks["task-a"].attempts_used == 2
    assert after.budgets == {"max_fix_attempts": 1}
    assert after.interrupts["ir-1"].resolved_action is None
    assert after.latest_checkpoint_id == "cp-1"

    # 下一次 Plan 所需投影也可从 ledger 完整重建。
    view = project_workflow_state(after)
    assert view.budgets == {"max_fix_attempts": 1}
    assert [i.interrupt_id for i in view.pending_interrupts] == ["ir-1"]
    assert view.nodes == {"node-a": ("task-a",)}
