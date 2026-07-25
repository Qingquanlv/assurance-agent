"""Unit tests for operation:retro-collect and operation:retro-accept handlers."""

from __future__ import annotations

import json
from pathlib import Path


from assurance_agent.workflow.graph.handlers.retro_ops import retro_accept, retro_collect
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.graph.contracts import ResourceClaims
from tests.helpers_aa import write_aa_config


def _make_workspace(tmp_path: Path) -> TaskWorkspace:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    write_aa_config(project)
    return TaskWorkspace(
        task_id="task-1",
        root=project,
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        base_tree_id="no-tree",
    )


def _make_context(workspace: TaskWorkspace, *, params: dict | None = None) -> RuntimeContext:
    return RuntimeContext(
        project_root=workspace.project_root,
        repo_root=workspace.repo_root,
        change_dir=workspace.change_dir,
        change_id="CH-1",
        params=params or {},
    )


def _make_task(target: str) -> ExecutableTask:
    return ExecutableTask(
        task_id="task-1",
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        graph_id="main",
        node_id="node-a",
        structural_path="main",
        input={"with": {}, "context": {"change_id": "CH-1"}},
        input_sha256="sha256-in",
        contract_digest="cd-1",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=60.0, heartbeat_seconds=10.0),
        target=target,
        resources=ResourceClaims(),
    )


def _write_context_json(retro_dir: Path, retro_id: str) -> None:
    """Write a minimal valid RetroContext to retro_dir/context.json.

    Carries evidence ``ev-1`` because accept refuses proposals citing evidence the
    context never collected.
    """
    retro_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "retro_id": retro_id,
        "generated_at": "2026-07-25T00:00:00Z",
        "window": {"change_count": 1, "change_ids": ["CH-1"], "change_sources": []},
        "signals": {
            "failure_distribution": [
                {"category": "assertion", "count": 1, "changes": ["CH-1"], "evidence_ids": ["ev-1"]}
            ],
            "gate_pushback": [],
            "healing_efficiency": {"attempts": 0, "applied": 0, "success_rate": 0.0, "evidence_ids": []},
            "human_decisions": [],
            "reclassifications": [],
            "skill_execution": [],
            "eval_trend": [],
        },
        "signal_count": 1,
    }
    (retro_dir / "context.json").write_text(json.dumps(ctx), encoding="utf-8")


# ---------------------------------------------------------------------------
# retro-collect
# ---------------------------------------------------------------------------


def _write_terminal_archived_change(
    project_root: Path,
    change_id: str,
    *,
    terminal_ts: str = "2026-07-25T12:00:00Z",
) -> Path:
    """Archive Change with an authoritative terminal ledger event (for last-N windows)."""
    write_aa_config(project_root)
    root = project_root / "qa" / "archive" / change_id
    root.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "seq": 1,
            "ts": "2026-07-25T11:00:00Z",
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
        },
        {
            "seq": 2,
            "ts": terminal_ts,
            "source": "gate",
            "type": "gate_verdict",
            "gate": "case-review",
            "verdict": "needs_fix",
            "evidence": {},
            "reason": "unresolved",
        },
        {
            "seq": 3,
            "ts": terminal_ts,
            "source": "graph",
            "type": "graph_completed",
            "invocation_id": f"inv-{change_id}",
            "checkpoint_ns": f"inv-{change_id}",
            "reason": "settled",
        },
    ]
    (root / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    (root / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    return root


def test_retro_collect_op_writes_context_and_value(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    context = _make_context(workspace, params={"retro_id": "retro-001", "retro_last": 5})
    task = _make_task("operation:retro-collect")

    result = retro_collect(task, workspace, context)

    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert isinstance(result.value["signal_count"], int)
    assert result.value["retro_id"] == "retro-001"
    context_file = workspace.project_root / "qa" / "retro" / "retro-001" / "context.json"
    assert context_file.exists()
    payload = json.loads(context_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2"


def test_retro_collect_op_scans_host_root_not_task_workspace(tmp_path: Path) -> None:
    """Workflow/Issue evidence is read from the host root; context.json lands in workspace."""
    host = tmp_path / "host"
    _write_terminal_archived_change(host, "CH-ARCHIVED")
    workspace = _make_workspace(tmp_path)
    context = RuntimeContext(
        project_root=host,
        repo_root=host,
        change_dir=host / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"retro_id": "retro-host-001", "retro_last": 10},
    )

    result = retro_collect(_make_task("operation:retro-collect"), workspace, context)

    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    written = workspace.project_root / "qa" / "retro" / "retro-host-001" / "context.json"
    assert written.is_file(), "context.json must land in the workspace to be frozen"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2"
    assert payload["window"]["change_ids"] == ["CH-ARCHIVED"]
    assert payload["signal_count"] >= 1  # gate pushback from host ledger
    assert not (host / "qa" / "retro" / "retro-host-001").exists()


def test_retro_collect_op_missing_retro_id_is_invalid_input(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    context = _make_context(workspace, params={})
    task = _make_task("operation:retro-collect")

    result = retro_collect(task, workspace, context)

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


# ---------------------------------------------------------------------------
# retro-accept
# ---------------------------------------------------------------------------


def test_retro_accept_op_rewrites_legacy_proposals(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    retro_id = "retro-002"
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    _write_context_json(retro_dir, retro_id)

    # Legacy proposal shape: apply_kind + proposed_change, no finding_kind/payload.
    legacy = [
        {
            "id": "p1",
            "apply_kind": "memory_append",
            "proposed_change": "Update the prompt rule to handle edge cases.",
            "evidence_ids": ["ev-1"],
            "problem": "Prompt rule misses edge cases",
        }
    ]
    (retro_dir / "proposals.json").write_text(json.dumps(legacy), encoding="utf-8")

    context = _make_context(workspace, params={"retro_id": retro_id})
    task = _make_task("operation:retro-accept")

    result = retro_accept(task, workspace, context)

    assert result.status == "succeeded"
    rewritten = json.loads((retro_dir / "proposals.json").read_text(encoding="utf-8"))
    entries = rewritten.get("proposals", rewritten)
    assert isinstance(entries, list) and len(entries) == 1
    assert entries[0].get("finding_kind") is not None
    assert entries[0].get("payload") is not None
    assert (retro_dir / "review-queue.md").exists()


def test_retro_accept_op_unroutable_is_invalid_output(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    retro_id = "retro-003"
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    _write_context_json(retro_dir, retro_id)

    # apply_kind: "contract_field" is not in the ApplyKind Literal → unroutable.
    bad_proposals = [
        {
            "id": "p1",
            "apply_kind": "contract_field",
            "proposed_change": "Some change",
            "evidence_ids": ["ev-1"],
        }
    ]
    (retro_dir / "proposals.json").write_text(json.dumps(bad_proposals), encoding="utf-8")

    context = _make_context(workspace, params={"retro_id": retro_id})
    task = _make_task("operation:retro-accept")

    result = retro_accept(task, workspace, context)

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
