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
    fold_invocation_events,
    project_invocation,
    project_workflow_state,
    render_workflow_state_yaml,
)
from assurance_agent.workflow.graph.definition_pinning import is_definition_binding_replayable
from assurance_agent.workflow.graph.models import GraphProjection

_ROOT = "root"
_BRANCH = "branch"
_LEAF = "leaf"
_LEAF_NS = f"{_ROOT}/branch-node/{_BRANCH}/cycle-node/{_LEAF}"
_BRANCH_NS = f"{_ROOT}/branch-node/{_BRANCH}"
_PLAN_PATH = "change:plans/fuzz.yaml"
_ROOT_BASE = "tree-root"
_BRANCH_BASE = "tree-branch"
_LEAF_BASE = "tree-leaf"
_TARGET = "tree-target"
_TRANSITION = "rt-leaf-1"


def _started(inv: str = "inv-1", *, schema_version: int = 1) -> dict:
    payload = {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": inv,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "gd-1",
        "event_schema_version": schema_version,
        "contract_digests": {"skill:noop": "cd-1"},
        "params": {"run_mode": "full"},
        "params_sha256": "ps-1",
        "root_tree_id": "tree-0",
        "max_parallel_tasks": 2,
        "checkpoint_ns": inv,
        "structural_path": "main",
    }
    if schema_version >= 4:
        payload.update(
            {
                "ir_digest": "gd-1",
                "ingest_catalog_digest": "cat-1",
                "policy_digest": "a" * 64,
                "policy_origin": "project",
                "gate_semantics_digest": "b" * 64,
                "assurance_profile_digest": "c" * 64,
            }
        )
    return payload


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


def _activated(node_id: str, generation: int = 0, inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "node_activated",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "graph_id": "main",
        "node_id": node_id,
        "generation_ordinal": generation,
        "activation_id": f"{node_id}-g{generation}",
        "input_sha256": "in-1",
        "source_reads_sha256": {},
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


def _recovery(
    task_id: str,
    *,
    node_id: str = "node-a",
    generation: int = 0,
    error_kind: str = "internal",
    message: str = "boom",
    checkpoint_ns: str | None = None,
    graph_id: str = "main",
    inv: str = "inv-1",
) -> dict:
    return {
        "source": "graph",
        "type": "task_recovery_routed",
        "invocation_id": inv,
        "checkpoint_ns": checkpoint_ns or inv,
        "graph_id": graph_id,
        "node_id": node_id,
        "generation_ordinal": generation,
        "task_id": task_id,
        "error_kind": error_kind,
        "message": message,
        "via": "recover-a",
        "continue_to": "after-a",
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


def test_recovery_route_projects_without_rewriting_failed_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _activated("node-a"),
            _planned(["task-a"]),
            _begin("task-a", "node-a"),
            _failed("task-a"),
            _recovery("task-a"),
        ],
    )

    projection = project_invocation(change, "inv-1")
    task = projection.tasks["task-a"]
    assert task.status == "failed"
    assert task.error_kind == "internal"
    assert task.error == "boom"
    recovery = projection.recoveries["task-a"]
    assert recovery.task_id == "task-a"
    assert recovery.node_id == "node-a"
    assert recovery.generation_ordinal == 0
    assert recovery.error_kind == "internal"
    assert recovery.message == "boom"
    assert recovery.via == "recover-a"
    assert recovery.continue_to == "after-a"


@pytest.mark.parametrize(
    ("events", "match"),
    [
        ([_recovery("missing")], "unknown task"),
        (
            [
                _activated("node-a"),
                _planned(["task-a"]),
                _begin("task-a", "node-a"),
                _failed("task-a"),
                _recovery("task-a", node_id="other"),
            ],
            "node/generation",
        ),
        (
            [
                _activated("node-a"),
                _planned(["task-a"]),
                _begin("task-a", "node-a"),
                _failed("task-a"),
                _recovery("task-a", generation=1),
            ],
            "node/generation",
        ),
        (
            [
                _activated("node-a"),
                _planned(["task-a"]),
                _begin("task-a", "node-a"),
                _failed("task-a"),
                _recovery("task-a", message="different"),
            ],
            "error context",
        ),
    ],
)
def test_recovery_route_requires_matching_failed_attempt(
    tmp_path: Path, events: list[dict], match: str
) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), *events])
    with pytest.raises(LedgerIntegrityError, match=match):
        project_invocation(change, "inv-1")


def test_duplicate_identical_recovery_route_is_integrity_error(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    recovery = _recovery("task-a")
    _append_all(
        change,
        [
            _started(),
            _activated("node-a"),
            _planned(["task-a"]),
            _begin("task-a", "node-a"),
            _failed("task-a"),
            recovery,
            recovery,
        ],
    )
    with pytest.raises(LedgerIntegrityError, match="duplicate.*recovery"):
        project_invocation(change, "inv-1")


@pytest.mark.parametrize(
    "recovery",
    [
        _recovery("task-a", checkpoint_ns="foreign-ns"),
        _recovery("task-a", graph_id="foreign-graph"),
    ],
)
def test_recovery_route_rejects_foreign_invocation_identity(tmp_path: Path, recovery: dict) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _activated("node-a"),
            _planned(["task-a"]),
            _begin("task-a", "node-a"),
            _failed("task-a"),
            recovery,
        ],
    )

    with pytest.raises(LedgerIntegrityError, match="canonical identity"):
        project_invocation(change, "inv-1")


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


def test_latest_root_invocation_scoped_by_entrypoint(tmp_path: Path) -> None:
    """Standalone entrypoints get their own root invocation on the same change."""
    change = tmp_path / "CH-1"
    change.mkdir()
    archive = {
        **_started("inv-2"),
        "entrypoint": "archive",
        "graph_id": "archive",
        "checkpoint_ns": "inv-2",
    }
    _append_all(change, [_started(), _terminal(), archive])

    store = CheckpointStore(change)
    assert store.latest_root_invocation() == "inv-2"
    assert store.latest_root_invocation("full") == "inv-1"
    assert store.latest_root_invocation("archive") == "inv-2"
    assert store.latest_root_invocation("retro") is None


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
    assert yaml.safe_load(state_file.read_text(encoding="utf-8")) == yaml.safe_load(original.decode("utf-8"))

    # 删除投影文件后从 strict ledger 重建：字节在语义上完全一致。
    state_file.unlink()
    assert not state_file.exists()
    rebuilt = render_workflow_state_yaml(project_invocation(change, "inv-1"))
    with transaction(change) as txn:
        txn.set_workflow_state_projection(rebuilt)
    assert yaml.safe_load(state_file.read_text(encoding="utf-8")) == yaml.safe_load(original.decode("utf-8"))


def test_projection_folds_v4_definition_binding_fields(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(schema_version=4)])

    projection = project_invocation(change, "inv-1")
    assert projection.event_schema_version == 4
    assert projection.policy_digest == "a" * 64
    assert projection.policy_origin == "project"
    assert projection.gate_semantics_digest == "b" * 64
    assert projection.assurance_profile_digest == "c" * 64
    assert is_definition_binding_replayable(projection)


def test_legacy_projection_is_not_replayable(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(schema_version=2)])

    projection = project_invocation(change, "inv-1")
    assert projection.event_schema_version == 2
    assert projection.policy_origin == ""
    assert not is_definition_binding_replayable(projection)


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


def _nested_started(
    inv: str,
    *,
    tree_id: str,
    checkpoint_ns: str,
    schema_version: int = 5,
    parent_invocation_id: str | None = None,
    parent_task_id: str | None = None,
    structural_path: str = "main",
) -> dict:
    event = _started(inv, schema_version=schema_version)
    event["root_tree_id"] = tree_id
    event["checkpoint_ns"] = checkpoint_ns
    event["parent_invocation_id"] = parent_invocation_id
    event["parent_task_id"] = parent_task_id
    event["structural_path"] = structural_path
    return event


def _nested_interrupted(
    *,
    inv: str,
    checkpoint_ns: str,
    interrupt_id: str = "ir-1",
    owner: str = _LEAF,
    with_source_pair: bool = True,
) -> dict:
    payload: dict[str, object] = {
        "source": "graph",
        "type": "graph_interrupted",
        "invocation_id": inv,
        "checkpoint_ns": checkpoint_ns,
        "interrupt_id": interrupt_id,
        "node_id": "gate",
        "checkpoint": "fuzz-plan-gate",
        "actions": ["fix_and_proceed", "accept_risk", "stop"],
        "audited_reads_sha256": {_PLAN_PATH: "a" * 64},
        "artifact_view": None,
        "revision_owner_invocation_id": owner,
        "revision_base_tree_id": _LEAF_BASE,
        "revision_view": f".graph-runtime/revision-views/{interrupt_id}",
        "revision_paths": [_PLAN_PATH],
        "revision_before_sha256": {_PLAN_PATH: "b" * 64},
    }
    if with_source_pair:
        payload["source_gate_attempt_id"] = "ga-1"
        payload["source_gate_tree_id"] = "tree-src"
    return payload


def _resume_anchor(inv: str, checkpoint_ns: str, interrupt_id: str = "ir-1") -> dict:
    return {
        "invocation_id": inv,
        "checkpoint_ns": checkpoint_ns,
        "node_id": "gate" if inv == _LEAF else "cycle-node" if inv == _BRANCH else "branch-node",
        "interrupt_id": interrupt_id,
    }


def _manual_revision(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "source": "graph",
        "type": "manual_plan_revision",
        "invocation_id": _LEAF,
        "checkpoint_ns": _LEAF_NS,
        "revision_transition_id": _TRANSITION,
        "interrupt_id": "ir-1",
        "action": "fix_and_proceed",
        "who": "reviewer",
        "reason": "fix plan",
        "audited_reads_sha256": {_PLAN_PATH: "a" * 64},
        "source_gate_attempt_id": "ga-1",
        "source_gate_tree_id": "tree-src",
        "base_tree_id": _LEAF_BASE,
        "target_tree_id": _TARGET,
        "logical_paths": [_PLAN_PATH],
        "before_sha256": {_PLAN_PATH: "b" * 64},
        "after_sha256": {_PLAN_PATH: "c" * 64},
        "resume_anchors": [
            _resume_anchor(_ROOT, _ROOT),
            _resume_anchor(_BRANCH, _BRANCH_NS),
            _resume_anchor(_LEAF, _LEAF_NS),
        ],
    }
    payload.update(overrides)
    return payload


def _nested_resumed(
    *,
    inv: str,
    checkpoint_ns: str,
    ordinal: int,
    chain_length: int = 3,
    interrupt_id: str = "ir-1",
    with_source_pair: bool = True,
    with_revision: bool = True,
) -> dict:
    payload: dict[str, object] = {
        "source": "graph",
        "type": "graph_resumed",
        "invocation_id": inv,
        "checkpoint_ns": checkpoint_ns,
        "interrupt_id": interrupt_id,
        "action": "fix_and_proceed",
        "reason": "fix plan",
        "who": "reviewer",
        "audited_reads_sha256": {_PLAN_PATH: "a" * 64} if inv == _LEAF else {},
        "anchor": _resume_anchor(inv, checkpoint_ns, interrupt_id),
    }
    if with_revision:
        payload["revision_transition_id"] = _TRANSITION
        payload["revision_ordinal"] = ordinal
        payload["revision_chain_length"] = chain_length
    if with_source_pair:
        payload["source_gate_attempt_id"] = "ga-1"
        payload["source_gate_tree_id"] = "tree-src"
    return payload


def _root_branch_leaf_prefix(*, schema_version: int = 5) -> list[dict]:
    return [
        _nested_started(_ROOT, tree_id=_ROOT_BASE, checkpoint_ns=_ROOT, schema_version=schema_version),
        _nested_started(
            _BRANCH,
            tree_id=_BRANCH_BASE,
            checkpoint_ns=_BRANCH_NS,
            schema_version=schema_version,
            parent_invocation_id=_ROOT,
            parent_task_id="branch-task",
            structural_path="main/branch-node",
        ),
        _nested_started(
            _LEAF,
            tree_id=_LEAF_BASE,
            checkpoint_ns=_LEAF_NS,
            schema_version=schema_version,
            parent_invocation_id=_BRANCH,
            parent_task_id="cycle-task",
            structural_path="main/branch-node/cycle-node",
        ),
        _nested_interrupted(inv=_LEAF, checkpoint_ns=_LEAF_NS),
        _nested_interrupted(inv=_BRANCH, checkpoint_ns=_BRANCH_NS),
        _nested_interrupted(inv=_ROOT, checkpoint_ns=_ROOT),
    ]


def test_manual_revision_fold_advances_only_leaf_tree() -> None:
    events = [
        *_root_branch_leaf_prefix(),
        _manual_revision(),
        _nested_resumed(inv=_ROOT, checkpoint_ns=_ROOT, ordinal=0),
        _nested_resumed(inv=_BRANCH, checkpoint_ns=_BRANCH_NS, ordinal=1),
        _nested_resumed(inv=_LEAF, checkpoint_ns=_LEAF_NS, ordinal=2),
    ]
    leaf = fold_invocation_events(_LEAF, events)
    branch = fold_invocation_events(_BRANCH, events)
    root = fold_invocation_events(_ROOT, events)
    assert leaf.current_tree_id == _TARGET
    assert branch.current_tree_id == _BRANCH_BASE
    assert root.current_tree_id == _ROOT_BASE
    assert leaf.interrupts["ir-1"].resolved_action == "fix_and_proceed"
    assert leaf.interrupts["ir-1"].revision_owner_invocation_id == _LEAF
    assert leaf.interrupts["ir-1"].source_gate_attempt_id == "ga-1"


@pytest.mark.parametrize(
    ("invocation_id", "events", "match"),
    [
        (
            _LEAF,
            [
                *_root_branch_leaf_prefix(),
                _manual_revision(interrupt_id="missing"),
            ],
            "unresolved interrupt",
        ),
        (
            _LEAF,
            [
                *_root_branch_leaf_prefix(),
                _manual_revision(),
                _nested_resumed(inv=_LEAF, checkpoint_ns=_LEAF_NS, ordinal=2),
                _manual_revision(revision_transition_id="rt-2", target_tree_id="tree-2"),
            ],
            "unresolved interrupt",
        ),
        (
            _BRANCH,
            [
                *_root_branch_leaf_prefix(),
                {
                    **_manual_revision(),
                    "invocation_id": _BRANCH,
                    "checkpoint_ns": _BRANCH_NS,
                    "base_tree_id": _BRANCH_BASE,
                },
            ],
            "owner",
        ),
        (
            _LEAF,
            [*_root_branch_leaf_prefix(), _manual_revision(base_tree_id="wrong-base")],
            "base_tree",
        ),
        (
            _LEAF,
            [*_root_branch_leaf_prefix(), _manual_revision(target_tree_id=_LEAF_BASE)],
            "target_tree",
        ),
        (
            _LEAF,
            [
                *_root_branch_leaf_prefix(),
                _manual_revision(before_sha256={_PLAN_PATH: "z" * 64}),
            ],
            "digest",
        ),
        (
            _LEAF,
            [
                *_root_branch_leaf_prefix(),
                _nested_resumed(inv=_LEAF, checkpoint_ns=_LEAF_NS, ordinal=2),
            ],
            "revision transition",
        ),
        (
            _LEAF,
            [
                *_root_branch_leaf_prefix(),
                _manual_revision(),
                _nested_resumed(inv=_LEAF, checkpoint_ns=_LEAF_NS, ordinal=2),
                {
                    **_nested_interrupted(
                        inv=_LEAF,
                        checkpoint_ns=_LEAF_NS,
                        interrupt_id="ir-2",
                    ),
                    "revision_base_tree_id": _TARGET,
                    "revision_before_sha256": {_PLAN_PATH: "c" * 64},
                },
                _manual_revision(
                    interrupt_id="ir-2",
                    revision_transition_id=_TRANSITION,
                    base_tree_id=_TARGET,
                    target_tree_id="tree-2",
                    before_sha256={_PLAN_PATH: "c" * 64},
                    after_sha256={_PLAN_PATH: "d" * 64},
                ),
            ],
            "revision transition",
        ),
    ],
)
def test_manual_revision_fold_rejects_invalid_lineage(
    invocation_id: str, events: list[dict], match: str
) -> None:
    with pytest.raises(LedgerIntegrityError, match=match):
        fold_invocation_events(invocation_id, events)


def test_v5_resume_source_pair_must_match_interrupt_pair() -> None:
    events = [
        *_root_branch_leaf_prefix(),
        _nested_resumed(
            inv=_LEAF,
            checkpoint_ns=_LEAF_NS,
            ordinal=0,
            chain_length=1,
            with_revision=False,
            with_source_pair=False,
        ),
    ]
    # pairless resume against paired interrupt
    with pytest.raises(LedgerIntegrityError, match="source_gate"):
        fold_invocation_events(_LEAF, events)

    paired_resume = _nested_resumed(
        inv=_LEAF,
        checkpoint_ns=_LEAF_NS,
        ordinal=0,
        chain_length=1,
        with_revision=False,
        with_source_pair=True,
    )
    paired_resume["source_gate_attempt_id"] = "other"
    with pytest.raises(LedgerIntegrityError, match="source_gate"):
        fold_invocation_events(
            _LEAF,
            [*_root_branch_leaf_prefix(), paired_resume],
        )


def test_v5_pairless_interrupt_requires_pairless_resume() -> None:
    prefix = _root_branch_leaf_prefix()
    # replace leaf interrupt with pairless form
    events = [
        event
        if not (event.get("type") == "graph_interrupted" and event.get("invocation_id") == _LEAF)
        else _nested_interrupted(inv=_LEAF, checkpoint_ns=_LEAF_NS, with_source_pair=False)
        for event in prefix
    ]
    with pytest.raises(LedgerIntegrityError, match="source_gate"):
        fold_invocation_events(
            _LEAF,
            [
                *events,
                _nested_resumed(
                    inv=_LEAF,
                    checkpoint_ns=_LEAF_NS,
                    ordinal=0,
                    chain_length=1,
                    with_revision=False,
                    with_source_pair=True,
                ),
            ],
        )
    projection = fold_invocation_events(
        _LEAF,
        [
            *events,
            _nested_resumed(
                inv=_LEAF,
                checkpoint_ns=_LEAF_NS,
                ordinal=0,
                chain_length=1,
                with_revision=False,
                with_source_pair=False,
            ),
        ],
    )
    assert projection.interrupts["ir-1"].source_gate_attempt_id is None
    assert projection.interrupts["ir-1"].resolved_action == "fix_and_proceed"


def test_v4_epoch_rejects_manual_revision_and_revision_tagged_resume() -> None:
    v4_prefix = _root_branch_leaf_prefix(schema_version=4)
    with pytest.raises(LedgerIntegrityError, match="event_schema_version"):
        fold_invocation_events(_LEAF, [*v4_prefix, _manual_revision()])
    with pytest.raises(LedgerIntegrityError, match="event_schema_version"):
        fold_invocation_events(
            _LEAF,
            [
                *v4_prefix,
                _nested_resumed(inv=_LEAF, checkpoint_ns=_LEAF_NS, ordinal=2),
            ],
        )


def test_candidate_validation_receipt_id_folds_optionally() -> None:
    historical = fold_invocation_events(
        "inv-1",
        [_started(), _planned(["t1"]), _begin("t1", "node-a"), _succeeded("t1", "ws-1")],
    )
    assert historical.tasks["t1"].candidate_validation_receipt_id is None

    with_receipt = dict(_succeeded("t1", "ws-1"))
    with_receipt["candidate_validation_receipt_id"] = "a" * 64
    projection = fold_invocation_events(
        "inv-1",
        [_started(), _planned(["t1"]), _begin("t1", "node-a"), with_receipt],
    )
    assert projection.tasks["t1"].candidate_validation_receipt_id == "a" * 64
