"""Unit tests for operation:retro-collect and operation:retro-accept handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
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


def test_retro_collect_op_maps_issue_history_integrity_to_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Product path: IssueHistoryIntegrityError → invalid_input (not a replaced op)."""
    from assurance_agent.workflow.issues.history import IssueHistoryIntegrityError

    def _boom(*_a: object, **_k: object) -> object:
        raise IssueHistoryIntegrityError("corrupt Issue Ledger")

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.handlers.retro_ops.run_retro_collect",
        _boom,
    )
    workspace = _make_workspace(tmp_path)
    context = _make_context(workspace, params={"retro_id": "retro-corrupt", "retro_last": 5})
    result = retro_collect(_make_task("operation:retro-collect"), workspace, context)

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "corrupt Issue Ledger" in (result.error or "")


# ---------------------------------------------------------------------------
# retro-accept
# ---------------------------------------------------------------------------


def _write_v2_context_json(
    retro_dir: Path, retro_id: str, *, evidence_ids: tuple[str, ...] = ("PROB-1",)
) -> dict:
    """Write a schema-v2 RetroContext used by Improvement Candidate accept."""
    from assurance_agent.retro.candidates import context_sha256
    from assurance_agent.retro.types import (
        EvalRetroSignals,
        IssueRetroSignals,
        RetroContext,
        RetroIntegrity,
        RetroSelectionSnapshot,
        RetroSignalSet,
        RetroSourceDescriptor,
        RetroSourceManifest,
        RetroWindow,
        WorkflowRetroSignals,
    )

    ctx = RetroContext(
        retro_id=retro_id,
        generated_at="2026-07-25T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-1",)),
            change_ids=("CH-1",),
        ),
        source_manifest=RetroSourceManifest(
            issue_slice_sha256="sha256:slice",
            issue_sources=(
                RetroSourceDescriptor(
                    kind="change_issue_ledger",
                    change_id="CH-1",
                    sha256="sha256:issue",
                    evidence_ids=evidence_ids,
                ),
            ),
            workflow_sources=(),
            eval_sources=(),
        ),
        integrity=RetroIntegrity(status="complete"),
        signals=RetroSignalSet(
            issue=IssueRetroSignals(),
            workflow=WorkflowRetroSignals(),
            eval=EvalRetroSignals(),
        ),
        signal_count=0,
    )
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "context.json").write_text(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"context": ctx, "context_sha256": context_sha256(ctx)}


def test_retro_accept_op_accepts_valid_candidates(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    retro_id = "retro-002"
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    meta = _write_v2_context_json(retro_dir, retro_id)
    document = {
        "schema_version": "2",
        "retro_id": retro_id,
        "context_sha256": meta["context_sha256"],
        "candidates": [
            {
                "candidate_id": "IMP-CAND-1",
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "assurance_agent/workflow/inspect",
                "rationale": "Repeated truncation across changes",
                "proposed_change": "Preserve pytest E lines when classifying failures",
                "verification": {
                    "suites": ["workflow-full"],
                    "success_criteria": "No truncation Observation",
                },
                "risk": "low",
                "confidence": "high",
            }
        ],
    }
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(document, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    context = _make_context(workspace, params={"retro_id": retro_id})
    task = _make_task("operation:retro-accept")

    result = retro_accept(task, workspace, context)

    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert result.value["result"] == "accepted"
    assert (retro_dir / "accept-status.json").is_file()
    assert (retro_dir / "review-queue.md").is_file()


def test_retro_accept_op_failed_receipt_is_invalid_output(tmp_path: Path) -> None:
    """Failed reconcile receipts must surface as graph task failure (fail-visible)."""
    workspace = _make_workspace(tmp_path)
    retro_id = "retro-003"
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    meta = _write_v2_context_json(retro_dir, retro_id)
    # Schema-valid Candidate with missing Problem evidence → semantic failed receipt.
    document = {
        "schema_version": "2",
        "retro_id": retro_id,
        "context_sha256": meta["context_sha256"],
        "candidates": [
            {
                "candidate_id": "IMP-CAND-1",
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "source_refs": {"problem_ids": ["PROB-MISSING"]},
                "target": "assurance_agent/workflow/inspect",
                "rationale": "Repeated truncation",
                "proposed_change": "Preserve pytest E lines",
                "verification": {
                    "suites": ["workflow-full"],
                    "success_criteria": "No truncation",
                },
                "risk": "low",
                "confidence": "high",
            }
        ],
    }
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(document, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    context = _make_context(workspace, params={"retro_id": retro_id})
    task = _make_task("operation:retro-accept")

    result = retro_accept(task, workspace, context)

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    status = json.loads((retro_dir / "accept-status.json").read_text(encoding="utf-8"))
    assert status["result"] == "failed"
