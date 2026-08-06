"""Graph-level archive workflow tests: the gate must decide before evidence moves.

``archive`` declares ``project:qa/archive/<change_id>/`` as a required output, so a
gate attached to that node can only ever report a verdict after the evidence was
copied — and the retry policy would push a refusing agent to produce the directory
anyway. These tests pin the routed shape: gate first, ``stop`` reaches STOP without
the archiver ever running.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from assurance_agent import resources
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.models import CompiledWorkflow, RuntimeContext
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-ARCHIVE-1"
T0 = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"archiver must not run; target={request.target}")


class FakeArchiver:
    """Stands in for ``aa-archive``: writes the declared output directory."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        archive_dir = request.workspace_root / "qa" / "archive" / CHANGE_ID
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "archive-summary.md").write_text("# archived\n", encoding="utf-8")
        # Copy issues/** when present in the source change directory.
        issues_src = request.workspace_root / "qa" / "changes" / CHANGE_ID / "issues"
        if issues_src.is_dir():
            import shutil

            shutil.copytree(issues_src, archive_dir / "issues", dirs_exist_ok=True)
        return AgentResult(ok=True)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def _seed_change(tmp_path: Path, *, final_status: str, healing_status: str) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / CHANGE_ID
    (change / "execution").mkdir(parents=True)
    (change / "inspect").mkdir()
    (change / "healing").mkdir()
    (change / "review").mkdir()
    write_aa_config(project)
    (change / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump({"batch_id": "b1", "final_status": final_status}), encoding="utf-8"
    )
    (change / "inspect" / "failure-analysis.json").write_text(
        json.dumps({"source_batch_id": "b1", "failures": []}), encoding="utf-8"
    )
    (change / "inspect" / "trace-sufficiency.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "authoritative_batch_id": "b1",
                "policy_digest": "0" * 64,
                "as_of": "2026-07-25T00:00:00+00:00",
                "integrity": "complete",
                "integrity_blocks_routing": False,
                "sufficient": True,
                "has_open_problems": False,
                "error_code": None,
                "insufficient_cases": [],
                "gap_codes": [],
            }
        ),
        encoding="utf-8",
    )
    (change / "healing" / "status.json").write_text(json.dumps({"status": healing_status}), encoding="utf-8")
    for name in ("case-review", "api-plan-review", "plan-review"):
        (change / "review" / f"{name}.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    (change / "inspect" / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "change_id": CHANGE_ID,
                "cadence": "pr",
                "computed_at": "2026-07-25T00:00:00+00:00",
                "risk_tier": "low",
                "risk_tier_lower_bound": "low",
                "risk_tier_declared": None,
                "risk_declaration_lowered": False,
                "risk_lowered_declarations": [],
                "metrics": {},
                "collection_gaps": [],
                "shortboards": [],
                "floor_ratio": None,
                "policy_digest": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    return project


def _compile_canonical() -> tuple[CompiledWorkflow, object]:
    contracts = load_execution_contracts(Path("."))
    compiled = compile_workflow(
        parse_workflow_v2(resources.read_text("schemas", "workflow-schema.yaml")), contracts
    )
    return compiled, contracts


def _build_runtime(
    project: Path, compiled: CompiledWorkflow, contracts: object, invoker: object
) -> GraphRuntime:
    change_dir = project / "qa" / "changes" / CHANGE_ID
    store = TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    node_runner = build_default_node_runner(
        invoker,  # type: ignore[arg-type]
        store,
        contracts,  # type: ignore[arg-type]
        compiled=compiled,
        run_child=run_child,
    )
    state_defs: dict = {}
    for graph in compiled.schema.graphs.values():
        state_defs.update(dict(graph.state))
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=FakeClock(),
        workspace_backend=workspaces,
        node_runner=node_runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,  # type: ignore[arg-type]
        state_defs=state_defs,
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,  # type: ignore[arg-type]
        node_runner=node_runner,
        scheduler=scheduler,
        schema_resolver=lambda digest: {compiled.digest: compiled}[digest],
        clock=FakeClock(),
    )
    holder["rt"] = runtime
    return runtime


def _context(project: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={"auto_archive": True, "test_types": ["api", "e2e"]},
    )


def test_archive_stops_before_running_the_archiver_on_failed_execution(tmp_path: Path) -> None:
    project = _seed_change(tmp_path, final_status="FAIL", healing_status="failed")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())

    result = runtime.run(compiled, "archive", _context(project))

    assert result.status.status == "stopped", result.reason
    assert "precheck" in (result.reason or "")
    assert not (project / "qa" / "archive").exists(), "a stop verdict must not copy evidence"
    activated = {
        json.loads(line).get("node_id")
        for line in (project / "qa" / "changes" / CHANGE_ID / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("type") == "node_activated"
    }
    assert activated == {"precheck"}


def test_archive_runs_the_archiver_when_the_gate_passes(tmp_path: Path) -> None:
    project = _seed_change(tmp_path, final_status="PASS", healing_status="not_needed")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, FakeArchiver())

    result = runtime.run(compiled, "archive", _context(project))

    assert result.exit_code == 0, result.reason
    assert (project / "qa" / "archive" / CHANGE_ID / "archive-summary.md").is_file()


def test_archive_stops_on_an_open_product_problem_even_when_execution_passed(
    tmp_path: Path,
) -> None:
    project = _seed_change(tmp_path, final_status="PASS", healing_status="not_needed")
    facts_path = project / "qa" / "changes" / CHANGE_ID / "inspect" / "trace-sufficiency.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts["has_open_problems"] = True
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())

    result = runtime.run(compiled, "archive", _context(project))

    assert result.status.status == "stopped", result.reason
    assert not (project / "qa" / "archive").exists()


def _seed_change_with_issues(
    tmp_path: Path,
    *,
    final_status: str,
    healing_status: str,
    issue_risk: str | None = None,
) -> Path:
    """Seed a change directory that also has issues/snapshot.json."""
    project = _seed_change(tmp_path, final_status=final_status, healing_status=healing_status)
    change = project / "qa" / "changes" / CHANGE_ID
    issues_dir = change / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    snapshot_payload: dict = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": "b1",
        "observations": [],
        "occurrences": [],
        "analysis_status": {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": "b1",
            "status": "completed" if issue_risk != "unknown" else "failed",
            "evidence_bundle_digest": "abc123",
            "candidate_count": 0,
        },
        "project_sync_status": "completed",
        "batches": ["b1"],
    }
    (issues_dir / "snapshot.json").write_text(json.dumps(snapshot_payload), encoding="utf-8")
    (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
    return project


def test_archive_copies_issues_directory_when_present(tmp_path: Path) -> None:
    """Archive copies issues/** to the archive directory."""
    project = _seed_change_with_issues(tmp_path, final_status="PASS", healing_status="not_needed")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, FakeArchiver())

    result = runtime.run(compiled, "archive", _context(project))

    assert result.exit_code == 0, result.reason
    archived_issues = project / "qa" / "archive" / CHANGE_ID / "issues"
    assert archived_issues.is_dir(), "issues/** must be copied to archive"
    assert (archived_issues / "snapshot.json").is_file()


def test_archive_does_not_block_on_open_issues(tmp_path: Path) -> None:
    """A critical open Problem must not stop archive — Issue state never blocks."""
    project = _seed_change_with_issues(tmp_path, final_status="PASS", healing_status="not_needed")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, FakeArchiver())

    # Archive must complete successfully even when issue snapshot exists with open risk.
    result = runtime.run(compiled, "archive", _context(project))
    assert result.exit_code == 0, result.reason


def test_archive_does_not_block_on_unknown_issue_risk(tmp_path: Path) -> None:
    """Unknown Issue risk (e.g. failed analysis) must not stop archive."""
    project = _seed_change_with_issues(
        tmp_path, final_status="PASS", healing_status="not_needed", issue_risk="unknown"
    )
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, FakeArchiver())

    result = runtime.run(compiled, "archive", _context(project))
    assert result.exit_code == 0, result.reason


def test_existing_execution_fail_gate_still_stops_archive_when_issues_present(tmp_path: Path) -> None:
    """Non-Issue precheck gates (execution FAIL) must still stop archive."""
    project = _seed_change_with_issues(tmp_path, final_status="FAIL", healing_status="failed")
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())

    result = runtime.run(compiled, "archive", _context(project))
    assert result.status.status == "stopped", result.reason
    assert not (project / "qa" / "archive").exists()
