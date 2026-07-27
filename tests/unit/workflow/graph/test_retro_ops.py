"""Unit tests for Retro v3 collection and Improvement reconcile handlers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.workflow.graph.handlers.retro_ops import (
    _selection_from_params,
    reconcile_improvements,
    retro_accept,
    retro_collect_v3,
)
from assurance_agent.retro.candidates import CandidateBatchInvalid, CandidateValidationError
from assurance_agent.retro.fallback import materialize_pipeline_failure_fallback
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


def test_retro_collect_v3_writes_window_and_three_typed_slices(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    _write_terminal_archived_change(workspace.project_root, "CH-ARCHIVED")
    host = tmp_path / "host-v3"
    host.mkdir()
    write_aa_config(host)
    context = RuntimeContext(
        project_root=host,
        repo_root=host,
        change_dir=host / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"retro_id": "retro-v3", "retro_last": 10},
    )

    result = retro_collect_v3(_make_task("operation:retro-collect-v3"), workspace, context)

    assert result.status == "succeeded", result.error
    retro_dir = workspace.project_root / "qa/retro/retro-v3"
    assert json.loads((retro_dir / "window.json").read_text())["change_ids"] == ["CH-ARCHIVED"]
    for domain in ("issue", "workflow", "eval"):
        payload = json.loads((retro_dir / "evidence" / f"{domain}-slice.json").read_text())
        assert payload["schema_version"] == "3"
        assert payload["domain"] == domain


def test_retro_params_preserve_explicit_batch_scope() -> None:
    batch_scope = {
        "batch_id": "batch-1",
        "status": "incomplete",
        "members": [
            {
                "change_id": "CH-1",
                "execution_status": "completed",
                "evidence_availability": "complete",
            },
            {
                "change_id": "CH-2",
                "execution_status": "running",
                "evidence_availability": "partial",
            },
        ],
    }
    selection = _selection_from_params({"change_ids": ["CH-1", "CH-2"], "batch_scope": batch_scope})
    assert selection.change_ids == ("CH-1", "CH-2")
    assert selection.batch_scope is not None
    assert selection.batch_scope.model_dump(mode="json") == {
        "schema_version": "1",
        **batch_scope,
    }


def test_retro_collect_reports_structured_batch_contract_failure(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    host = tmp_path / "host-batch-contract"
    host.mkdir()
    write_aa_config(host)
    context = RuntimeContext(
        project_root=host,
        repo_root=host,
        change_dir=host / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={
            "retro_id": "retro-invalid-batch",
            "change_ids": ["CH-2"],
            "batch_scope": {
                "batch_id": "batch-1",
                "status": "complete",
                "members": [
                    {
                        "change_id": "CH-1",
                        "execution_status": "completed",
                        "evidence_availability": "complete",
                    }
                ],
            },
        },
    )

    result = retro_collect_v3(_make_task("operation:retro-collect-v3"), workspace, context)

    assert result.status == "failed"
    assert result.error_kind == "batch_scope_invalid"


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


def test_reconcile_contract_error_is_not_hidden_as_pending(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    retro_id = "retro-contract-error"
    materialize_pipeline_failure_fallback(
        workspace.project_root,
        failure=RetroPipelineFailure(
            failure_id="FAIL-contract",
            retro_id=retro_id,
            stage="reconcile",
            node_id="reconcile-improvements",
            error_kind="invalid_output",
            message_fingerprint="sha256:" + "a" * 64,
            occurred_at=datetime.now(timezone.utc),
        ),
        batch_scope=None,
    )

    def fail_contract(_project_root: Path):
        raise CandidateBatchInvalid((CandidateValidationError(code="candidate_contract_failed"),))

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.handlers.retro_ops.drain_reconcile_outbox",
        fail_contract,
    )

    result = reconcile_improvements(
        _make_task("operation:reconcile-improvements"),
        workspace,
        _make_context(workspace, params={"retro_id": retro_id}),
    )

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
