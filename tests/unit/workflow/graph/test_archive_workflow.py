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
    (change / "healing" / "status.json").write_text(json.dumps({"status": healing_status}), encoding="utf-8")
    for name in ("case-review", "api-plan-review", "plan-review"):
        (change / "review" / f"{name}.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
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
