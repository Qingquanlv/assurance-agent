"""Integration tests for the inspect-with-issues subgraph topology.

Proves (using an inline schema that mirrors inspect-with-issues):
- observations commit before the analyzer starts
- analyzer timeout after retries completes full workflow with failed analysis status
  (fail-open: recovery node executes, graph completes successfully)
- analyzer forbidden_write fails the graph hard
- zero abnormal observations skips analyzer and runs record-empty-analysis
- run_tests == false skips execution and the Issue subgraph entirely
- Issue outcomes never alter execution manifest/quality-gate final_status

The inline schema mirrors the packaged inspect-with-issues graph structure but
uses simpler fake operations so the test is self-contained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.issues.identity import candidate_document_digest
from tests.helpers_aa import write_aa_config

# ---------------------------------------------------------------------------
# Inline contracts
# ---------------------------------------------------------------------------

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true

  operation:fake-inspect:
    handler: operation
    reads: ["change:execution/**"]
    writes: ["change:inspect/failure-analysis.json", "change:inspect/quality-gate-result.json"]
    authorization_writes: ["change:inspect/**"]
    retryable_errors: [timeout, transport]

  operation:fake-collect-observations:
    handler: operation
    reads: ["change:execution/**"]
    writes: [
      "change:inspect/observations.json",
      "change:inspect/issue-evidence-manifest.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
    ]
    authorization_writes: [
      "change:inspect/observations.json",
      "change:inspect/issue-evidence-manifest.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
    ]
    retryable_errors: [timeout, transport]

  operation:fake-analyzer:
    handler: operation
    reads: ["change:inspect/observations.json", "change:inspect/issue-evidence-manifest.json"]
    writes: ["change:inspect/issue-candidates.json", "change:inspect/issue-analysis-status.json"]
    authorization_writes: [
      "change:inspect/issue-candidates.json",
      "change:inspect/issue-analysis-status.json",
    ]
    retryable_errors: [timeout, transport, rate_limit, invalid_output]

  operation:fake-record-empty-analysis:
    handler: operation
    reads: ["change:inspect/issue-evidence-manifest.json"]
    writes: [
      "change:inspect/issue-candidates.json",
      "change:inspect/issue-analysis-status.json",
    ]
    authorization_writes: [
      "change:inspect/issue-candidates.json",
      "change:inspect/issue-analysis-status.json",
    ]
    retryable_errors: []

  operation:fake-reconcile-issues:
    handler: operation
    reads: ["change:inspect/**", "change:issues/**", "project:qa/issues/**"]
    writes: [
      "change:inspect/issue-reconcile-status.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
      "project:qa/issues/**",
    ]
    authorization_writes: [
      "change:inspect/issue-reconcile-status.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
      "project:qa/issues/**",
    ]
    synchronized: ["project:qa/issues/**"]
    exclusive: ["project:issue-registry"]
    retryable_errors: [conflict, transport]

  operation:fake-record-issue-analysis-failure:
    handler: operation
    reads: ["change:inspect/issue-evidence-manifest.json"]
    writes: [
      "change:inspect/issue-candidates.json",
      "change:inspect/issue-analysis-status.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
    ]
    authorization_writes: [
      "change:inspect/issue-candidates.json",
      "change:inspect/issue-analysis-status.json",
      "change:issues/events.jsonl",
      "change:issues/snapshot.json",
    ]
    retryable_errors: []

  operation:fake-record-project-sync-pending:
    handler: operation
    reads: ["change:inspect/issue-evidence-manifest.json", "change:inspect/issue-candidates.json"]
    writes: ["change:issues/events.jsonl", "change:issues/snapshot.json"]
    authorization_writes: ["change:issues/events.jsonl", "change:issues/snapshot.json"]
    retryable_errors: []

  operation:fake-materialize-trace-projection:
    handler: operation
    reads: ["change:inspect/**", "change:issues/**", "change:execution/**", "change:cases/**"]
    writes: ["change:inspect/trace-projection.json"]
    authorization_writes: ["change:inspect/trace-projection.json"]
    retryable_errors: []
"""

# ---------------------------------------------------------------------------
# Inline workflow (mirrors the inspect-with-issues schema structure)
# ---------------------------------------------------------------------------

_WORKFLOW = """\
name: iwi-test
params:
  run_mode: {type: enum, values: [full], default: full}
  run_tests: {type: bool, default: true}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
  nested-full: {graph: nested-main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
    llm-default:
      max_attempts: 3
      retry_on: [timeout, transport, rate_limit, invalid_output]
      backoff: {initial_seconds: 0.0, multiplier: 1.0, max_seconds: 0.0, jitter: false}
    project-sync:
      max_attempts: 3
      retry_on: [conflict, transport]
      backoff: {initial_seconds: 0.0, multiplier: 1.0, max_seconds: 0.0, jitter: false}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  nested-main:
    max_supersteps: 5
    nodes:
      assurance:
        uses: graph:main
    edges:
      - {from: START, to: assurance}
      - {from: assurance, to: END}

  main:
    max_supersteps: 10
    nodes:
      execution:
        uses: operation:fake-inspect
        retry: never
        timeout: local
      generation-join:
        uses: operation:no-op
      inspect-with-issues:
        uses: graph:inspect-with-issues
    edges:
      - {from: START, to: execution, when: "params.run_tests == true"}
      - {from: execution, to: generation-join}
      - {from: START, to: generation-join, when: "params.run_tests == false"}
      - {from: generation-join, to: inspect-with-issues, when: "params.run_tests == true"}
      - {from: generation-join, to: END, when: "params.run_tests == false"}
      - {from: inspect-with-issues, to: END}

  inspect-with-issues:
    max_supersteps: 15
    nodes:
      inspect:
        uses: operation:fake-inspect
        retry: never
        timeout: local

      collect-observations:
        uses: operation:fake-collect-observations
        retry: never
        timeout: local

      analyze-issues:
        uses: operation:fake-analyzer
        retry: llm-default
        timeout: local
        recover:
          errors: [timeout, transport, rate_limit, invalid_output]
          via: record-analysis-failure
          continue_to: materialize-trace-projection

      record-empty-analysis:
        uses: operation:fake-record-empty-analysis
        retry: never
        timeout: local

      reconcile-issues:
        uses: operation:fake-reconcile-issues
        retry: project-sync
        timeout: local
        recover:
          errors: [conflict, transport]
          via: record-project-sync-pending
          continue_to: materialize-trace-projection

      record-analysis-failure:
        uses: operation:fake-record-issue-analysis-failure
        retry: never
        timeout: local

      record-project-sync-pending:
        uses: operation:fake-record-project-sync-pending
        retry: never
        timeout: local

      materialize-trace-projection:
        uses: operation:fake-materialize-trace-projection
        outputs:
          - change:inspect/trace-projection.json

      inspect-complete:
        uses: operation:no-op

    edges:
      - {from: START, to: inspect}
      - {from: inspect, to: collect-observations}
      - {from: collect-observations, to: analyze-issues,
         when: "node('collect-observations').value.abnormal_count > 0"}
      - {from: collect-observations, to: record-empty-analysis,
         when: "node('collect-observations').value.abnormal_count == 0"}
      - {from: record-empty-analysis, to: reconcile-issues}
      - {from: analyze-issues, to: reconcile-issues}
      - {from: reconcile-issues, to: materialize-trace-projection}
      - {from: materialize-trace-projection, to: inspect-complete}
      - {from: inspect-complete, to: END}
gates: {}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "qa" / "issues").mkdir(parents=True)
    write_aa_config(project)
    return project


def _context(project: Path, *, params: dict[str, object] | None = None) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params=params or {"run_mode": "full"},
    )


def _compile() -> tuple[CompiledWorkflow, Any]:
    contracts = parse_execution_contracts(_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_WORKFLOW), contracts)
    return compiled, contracts


def _fake_inspect(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
    """Write minimal quality-gate artifacts to satisfy downstream reads."""
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "failure-analysis.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": context.change_id,
                "source_manifest": "execution/execution-manifest.json",
                "inspection_status": "completed",
                "batch_id": "batch-1",
                "source_batch_id": "batch-1",
                "final_status": "FAIL",
                "inspect_mode": "primary",
                "classification_performed": True,
                "status": "analyzed",
                "failures": [],
                "hard_fails": [],
                "needs_review": [],
                "known_product_issues": [],
            }
        ),
        encoding="utf-8",
    )
    (inspect_dir / "quality-gate-result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": context.change_id,
                "batch_id": "batch-1",
                "dimensions": {
                    "functional": {
                        "status": "FAIL",
                        "api": {"total": 1, "passed": 0, "failed": 1},
                        "e2e": {"total": 0, "passed": 0, "failed": 0},
                    },
                    "coverage": {
                        "status": "SKIPPED",
                        "available": False,
                        "line_coverage": 0.0,
                        "branch_coverage": 0.0,
                        "threshold": {"line": 0.0, "branch": 0.0},
                    },
                },
                "final_status": "FAIL",
            }
        ),
        encoding="utf-8",
    )
    return TaskResult(status="succeeded", value={"final_status": "FAIL"})


def _fake_collect_observations(abnormal_count: int) -> OperationFn:
    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        issues_dir = workspace.change_dir / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        (inspect_dir / "observations.json").write_text(
            json.dumps({"observations": [], "abnormal_count": abnormal_count}),
            encoding="utf-8",
        )
        (inspect_dir / "issue-evidence-manifest.json").write_text(
            json.dumps({"batch_id": "batch-1", "evidence_bundle_digest": "abc123"}),
            encoding="utf-8",
        )
        (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
        (issues_dir / "snapshot.json").write_text(
            json.dumps({"batch_id": "batch-1", "occurrences": []}), encoding="utf-8"
        )
        return TaskResult(
            status="succeeded",
            value={
                "batch_id": "batch-1",
                "evidence_bundle_digest": "abc123",
                "abnormal_count": abnormal_count,
            },
        )

    return _fn


def _write_fake_analysis(
    inspect_dir: Path,
    context: RuntimeContext,
    *,
    status: str,
    candidates: list[dict[str, object]] | None = None,
    reason: str | None = None,
) -> None:
    candidate_items = candidates or []
    evidence_digest = "sha256:abc123"
    candidate_document = {
        "schema_version": "1.0",
        "change_id": context.change_id,
        "batch_id": "batch-1",
        "evidence_bundle_digest": evidence_digest,
        "candidates": candidate_items,
    }
    (inspect_dir / "issue-candidates.json").write_text(
        json.dumps(candidate_document),
        encoding="utf-8",
    )
    status_document: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": context.change_id,
        "batch_id": "batch-1",
        "status": status,
        "evidence_bundle_digest": evidence_digest,
        "candidate_count": len(candidate_items),
        "candidate_digest": candidate_document_digest(candidate_document),
    }
    if reason is not None:
        status_document.update({"reason": reason, "retryable": True})
    (inspect_dir / "issue-analysis-status.json").write_text(
        json.dumps(status_document),
        encoding="utf-8",
    )


def _fake_record_empty_analysis(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    _write_fake_analysis(inspect_dir, context, status="completed")
    return TaskResult(status="succeeded", value={"candidate_count": 0})


def _fake_reconcile_issues(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    project_issues = workspace.project_root / "qa" / "issues"
    project_issues.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "issue-reconcile-status.json").write_text(
        json.dumps({"status": "completed", "occurrence_count": 0}), encoding="utf-8"
    )
    (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
    (issues_dir / "snapshot.json").write_text(json.dumps({"occurrences": [], "version": 1}), encoding="utf-8")
    (project_issues / "problems.json").write_text(
        json.dumps({"problems": [], "version": 1}), encoding="utf-8"
    )
    return TaskResult(status="succeeded", value={"occurrence_count": 0})


def _fake_record_analysis_failure(
    task: ExecutableTask, workspace: Any, context: RuntimeContext
) -> TaskResult:
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    _write_fake_analysis(inspect_dir, context, status="failed", reason="timeout")
    (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
    (issues_dir / "snapshot.json").write_text(
        json.dumps({"occurrences": [], "analysis_failed": True}), encoding="utf-8"
    )
    return TaskResult(status="succeeded", value={"analysis_failed": True})


def _fake_record_project_sync_pending(
    task: ExecutableTask, workspace: Any, context: RuntimeContext
) -> TaskResult:
    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
    (issues_dir / "snapshot.json").write_text(
        json.dumps({"occurrences": [], "sync_pending": True}), encoding="utf-8"
    )
    return TaskResult(status="succeeded", value={"sync_pending": True})


def _fake_materialize_trace_projection(
    task: ExecutableTask, workspace: Any, context: RuntimeContext
) -> TaskResult:
    del task, context
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "trace-projection.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "phase": "reconciled",
                "change_id": "CH-1",
                "authoritative_batch_id": "batch-1",
                "integrity": "incomplete",
                "sources": [],
                "rows": [],
                "unmapped_tests": [],
                "gaps": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return TaskResult(status="succeeded", value={"phase": "reconciled", "integrity": "incomplete"})


def _fake_analyzer_succeed(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
    """Analyzer that always succeeds."""
    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    _write_fake_analysis(inspect_dir, context, status="completed")
    return TaskResult(status="succeeded", value={"candidate_count": 0})


def _make_timeout_analyzer() -> tuple[OperationFn, dict[str, int]]:
    """Returns an analyzer that always times out, plus a call counter."""
    calls = {"n": 0}

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        calls["n"] += 1
        return TaskResult(status="failed", error_kind="timeout", error="timed out")

    return _fn, calls


def _make_forbidden_write_analyzer() -> OperationFn:
    """Returns an analyzer that returns a forbidden_write error."""

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="failed", error_kind="forbidden_write", error="wrote outside contract")

    return _fn


def _default_ops(
    abnormal_count: int = 1,
    analyzer: OperationFn | None = None,
) -> dict[str, OperationFn]:
    return {
        "operation:fake-inspect": _fake_inspect,
        "operation:fake-collect-observations": _fake_collect_observations(abnormal_count),
        "operation:fake-analyzer": analyzer or _fake_analyzer_succeed,
        "operation:fake-record-empty-analysis": _fake_record_empty_analysis,
        "operation:fake-reconcile-issues": _fake_reconcile_issues,
        "operation:fake-record-issue-analysis-failure": _fake_record_analysis_failure,
        "operation:fake-record-project-sync-pending": _fake_record_project_sync_pending,
        "operation:fake-materialize-trace-projection": _fake_materialize_trace_projection,
        "operation:no-op": lambda task, ws, ctx: TaskResult(status="succeeded"),
    }


def _build(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    *,
    ops: dict[str, OperationFn] | None = None,
) -> GraphRuntime:
    from assurance_agent.workflow.graph.handlers.gate import GateHandler
    from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
    from assurance_agent.workflow.graph.handlers.join import JoinHandler
    from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler

    def build_node_runner(store, run_child):  # type: ignore[no-untyped-def]
        op_handler = OperationHandler(ops or {})
        handlers: dict[str, Any] = {
            "builtin:join": JoinHandler(),
            "builtin:gate": GateHandler(compiled),
            "builtin:interrupt": InterruptHandler(compiled),
        }
        for target in ops or {}:
            handlers[target] = op_handler
        return HandlerNodeRunner(
            handlers,
            namespace_handlers={"graph": SubgraphHandler(run_child)},
            compiled=compiled,
            object_store=store,
        )

    return assemble_graph_runtime(
        project_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        compiled=compiled,
        contracts=contracts,
        operations=ops or {},
        build_node_runner=build_node_runner,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_nested_issue_subgraph_inherits_shared_project_lock(tmp_path: Path) -> None:
    """A graph task may reserve a descendant project token without deadlocking
    when the synchronized leaf executes under the same runtime lock manager."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()
    runtime = _build(
        project,
        compiled,
        contracts,
        ops=_default_ops(abnormal_count=0),
    )

    result = runtime.run(
        compiled,
        "nested-full",
        _context(project, params={"run_mode": "full", "run_tests": True}),
    )

    assert result.status.status == "completed", result.reason
    assert result.exit_code == 0
    assert list((project / "qa" / "changes" / "CH-1").rglob("issue-reconcile-status.json"))


def test_observations_commit_before_analyzer_starts(tmp_path: Path) -> None:
    """collect-observations must succeed and its outputs be committed before
    the analyzer operation is ever invoked."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    observations_committed_before_analyzer: list[bool] = []

    def _checking_analyzer(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        # Check that the observations file was committed to the canonical
        # change dir before the analyzer runs.
        change = project / "qa" / "changes" / "CH-1"
        obs_candidates = list(change.rglob("observations.json"))
        observations_committed_before_analyzer.append(len(obs_candidates) > 0)
        # Write analyzer outputs.
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        _write_fake_analysis(inspect_dir, context, status="completed")
        return TaskResult(status="succeeded", value={"candidate_count": 0})

    ops = _default_ops(abnormal_count=1, analyzer=_checking_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0
    assert observations_committed_before_analyzer, "analyzer was never called"
    assert all(observations_committed_before_analyzer), (
        "observations were not committed before analyzer invocation"
    )


def test_analyzer_timeout_fails_open_and_graph_completes(tmp_path: Path) -> None:
    """When the analyzer times out on all retry attempts (llm-default: 3),
    the recovery path (record-analysis-failure) executes and the graph completes
    successfully. Issue analysis status is 'failed' but exit_code is 0."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    timeout_analyzer, call_count = _make_timeout_analyzer()
    ops = _default_ops(abnormal_count=1, analyzer=timeout_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))

    # Graph must complete (fail-open: analysis failure is not a hard failure).
    assert result.exit_code == 0, f"expected exit_code 0, got {result.exit_code}: {result.status}"
    assert result.status.status == "completed"

    # Analyzer must have been retried max_attempts (3) times.
    assert call_count["n"] == 3, f"expected 3 analyzer calls, got {call_count['n']}"

    # Record-analysis-failure must have written the failed status artifact.
    change = project / "qa" / "changes" / "CH-1"
    analysis_files = list(change.rglob("issue-analysis-status.json"))
    assert analysis_files, "analysis status file was not written by recovery operation"
    status = json.loads(analysis_files[0].read_text(encoding="utf-8"))
    assert status.get("status") == "failed"

    # The graph must have emitted a task_recovery_routed event.
    events = read_events_strict(change)
    assert any(e.get("type") == "task_recovery_routed" for e in events), (
        "no task_recovery_routed event found after analyzer exhaustion"
    )


def test_analyzer_forbidden_write_fails_graph_hard(tmp_path: Path) -> None:
    """A forbidden_write error from the analyzer must fail the graph hard
    because forbidden_write is not in the recovery allowlist."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    forbidden_analyzer = _make_forbidden_write_analyzer()
    ops = _default_ops(abnormal_count=1, analyzer=forbidden_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code != 0
    assert result.status.status == "failed"


def test_zero_abnormal_count_skips_analyzer_runs_empty_analysis(tmp_path: Path) -> None:
    """When collect-observations finds no abnormal signals (abnormal_count == 0),
    the analyzer must never be called and record-empty-analysis runs instead."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    analyzer_called = {"n": 0}

    def _never_analyzer(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        analyzer_called["n"] += 1
        raise AssertionError("analyzer must not be called when abnormal_count == 0")

    ops = _default_ops(abnormal_count=0, analyzer=_never_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0
    assert analyzer_called["n"] == 0, "analyzer should not be called"
    change = project / "qa" / "changes" / "CH-1"
    analysis_files = list(change.rglob("issue-analysis-status.json"))
    assert analysis_files, "empty-analysis status file was not written"
    status = json.loads(analysis_files[0].read_text(encoding="utf-8"))
    assert status.get("candidate_count") == 0


def test_run_tests_false_produces_no_issue_subgraph_artifacts(tmp_path: Path) -> None:
    """run_tests=false must route generation-join -> END, skipping execution
    and the inspect-with-issues subgraph entirely — no Issue artifacts created."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    ops = _default_ops()
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(
        compiled,
        "full",
        _context(project, params={"run_mode": "full", "run_tests": False}),
    )

    assert result.exit_code == 0
    change = project / "qa" / "changes" / "CH-1"
    assert not list(change.rglob("observations.json")), "observations.json must not exist"
    assert not list(change.rglob("issue-candidates.json")), "issue-candidates.json must not exist"
    assert not list(change.rglob("issue-reconcile-status.json")), "issue-reconcile-status.json must not exist"


def test_issue_outcomes_do_not_alter_quality_gate_final_status(tmp_path: Path) -> None:
    """The execution final_status in failure-analysis.json must remain unchanged
    regardless of whether Issue analysis succeeds, finds candidates, or fails."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile()

    def _succeeding_analyzer(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        _write_fake_analysis(
            inspect_dir,
            context,
            status="completed",
            candidates=[
                {
                    "candidate_id": "C-1",
                    "observation_ids": ["OBS-1"],
                    "proposed": {
                        "title": "Observed product behavior",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "The observed request failed in the product path.",
                    },
                    "affected_surface": {"kind": "endpoint", "value": "GET /api/v1/test"},
                    "fingerprint_inputs": {
                        "surface": "GET /api/v1/test",
                        "symptom": "request_failed",
                    },
                    "possible_problem_ids": [],
                    "confidence": 0.9,
                    "recommended_action": "Investigate the product handler.",
                }
            ],
        )
        return TaskResult(status="succeeded", value={"candidate_count": 1})

    ops = _default_ops(abnormal_count=1, analyzer=_succeeding_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0
    change = project / "qa" / "changes" / "CH-1"
    # The failure-analysis.json final_status must be unchanged by Issue processing.
    analysis_files = list(change.rglob("failure-analysis.json"))
    assert analysis_files
    analysis = json.loads(analysis_files[0].read_text(encoding="utf-8"))
    assert analysis["final_status"] == "FAIL", "Issue analysis must not alter execution final_status"


def test_inline_main_graph_orders_inspect_before_end_without_retro() -> None:
    """Graph-order invariant: inspect-with-issues precedes END; no Retro nodes."""
    schema = parse_workflow_v2(_WORKFLOW)
    main = schema.graphs["main"]
    edges = {(edge.from_, edge.to) for edge in main.edges}
    assert ("generation-join", "inspect-with-issues") in edges
    assert ("inspect-with-issues", "END") in edges
    uses = {node.uses for node in main.nodes.values()}
    assert "graph:inspect-with-issues" in uses
    assert "graph:retro-workflow" not in uses
    assert "operation:reconcile-improvements" not in uses
    assert "skill:aa-retro" not in uses


def test_inline_inspect_routes_recovery_through_materializer() -> None:
    schema = parse_workflow_v2(_WORKFLOW)
    inspect = schema.graphs["inspect-with-issues"]
    edges = {(edge.from_, edge.to) for edge in inspect.edges}
    assert ("reconcile-issues", "materialize-trace-projection") in edges
    assert ("materialize-trace-projection", "inspect-complete") in edges
    analyze = inspect.nodes["analyze-issues"]
    assert analyze.recover is not None
    assert analyze.recover.continue_to == "materialize-trace-projection"
    reconcile = inspect.nodes["reconcile-issues"]
    assert reconcile.recover is not None
    assert reconcile.recover.continue_to == "materialize-trace-projection"


def test_analyzer_timeout_materializes_before_complete(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile()
    timeout_analyzer, calls = _make_timeout_analyzer()
    ops = _default_ops(abnormal_count=1, analyzer=timeout_analyzer)
    runtime = _build(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 0
    assert calls["n"] == 3
    change = project / "qa" / "changes" / "CH-1"
    assert (change / "inspect" / "trace-projection.json").is_file()
    events = read_events_strict(change)
    assert any(e.get("type") == "task_recovery_routed" for e in events)
