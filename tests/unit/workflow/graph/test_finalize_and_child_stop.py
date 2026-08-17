"""P0: attached-gate finalize on success + child STOP propagation."""

from __future__ import annotations

import json
import textwrap
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import OperationFn
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 20, 3, 0, 0, tzinfo=timezone.utc)

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:skill-registry-check:
    handler: operation
    reads:
      - project:skills/**
    writes:
      - change:registry/**
    authorization_writes:
      - change:registry/**
    retryable_errors: []
  operation:stop:
    handler: operation
    side_effect_free: true
  operation:explore-marker:
    handler: operation
    side_effect_free: false
    writes: ["change:explore/**"]
    authorization_writes: ["change:explore/**"]
    retryable_errors: []
  builtin:gate:
    handler: builtin
    side_effect_free: true
"""

_FOOTER = """
gates:
  registry-gate:
    reads: [{ path: registry/skill-registry-check.json, as: registry }]
    invalid_json: stop
    missing_field_is: stop
    pass_when: "registry.status == 'pass' and registry.healing_available == true"
    stop_when: "registry.status == 'fail' or registry.healing_available == false"
  leaf-review-gate:
    reads: [review/leaf.json]
    invalid_json: stop
    missing_field_is: stop
    needs_human_review_when: "leaf.decision == 'needs_human_review'"
    pass_when: "leaf.decision == 'pass'"
"""


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"agent must not run: {request.target}")


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


def _make_project(tmp_path: Path, *, skills: bool = True) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(project)
    if skills:
        for name in ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer"):
            skill_dir = project / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return project


def _compile(body: str) -> tuple[CompiledWorkflow, object]:
    text = (
        'name: t\n'
        "params:\n  run_mode: {type: enum, values: [full], default: full}\n"
        "  max_healing_attempts: {type: int, default: 3}\n"
        "entrypoints:\n  full: {graph: main, allow: \"params.run_mode == 'full'\"}\n"
        "policies:\n"
        "  retry:\n    never: {max_attempts: 1, retry_on: []}\n"
        "  timeout:\n    local: {run_seconds: 60, heartbeat_seconds: 0.05}\n"
        "  scheduler: {max_parallel_tasks: 4}\n"
        "graphs:\n" + textwrap.indent(textwrap.dedent(body), "  ") + _FOOTER
    )
    contracts = parse_execution_contracts(_CONTRACTS)
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


def _ops() -> dict[str, OperationFn]:
    ops = default_operations()

    def explore_marker(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "explore" / "advisory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:explore-marker"] = explore_marker
    return ops


def _runtime(project: Path, compiled: CompiledWorkflow, contracts) -> GraphRuntime:
    return assemble_graph_runtime(
        project_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        compiled=compiled,
        contracts=contracts,
        adapter=NeverCalledInvoker(),
        operations={**default_operations(), **_ops()},
        clock=FakeClock(),
    )


def _ctx(project: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"run_mode": "full", "max_healing_attempts": 3},
    )


def test_registry_node_freezes_attached_gate_report_and_routes_to_end(tmp_path: Path) -> None:
    """Bootstrap-shaped registry: outputs + attached gate must freeze gate_report=pass."""
    project = _make_project(tmp_path, skills=True)
    compiled, contracts = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            registry:
              uses: operation:skill-registry-check
              outputs: [change:registry/skill-registry-check.json]
              gate: registry-gate
              retry: never
              timeout: local
          edges:
            - {from: START, to: registry}
          routes:
            - from: registry
              select: "node('registry').gate.verdict"
              cases: {pass: END, stop: STOP}
              default: STOP
        """
    )
    runtime = _runtime(project, compiled, contracts)
    result = runtime.run(compiled, "full", _ctx(project))
    assert result.status.status == "completed", result.reason

    events = read_events_strict(project / "qa" / "changes" / "CH-1")
    succeeded = [e for e in events if e.get("type") == "task_attempt_succeeded"]
    assert succeeded
    gate_report = succeeded[0].get("gate_report")
    assert isinstance(gate_report, dict)
    assert gate_report.get("verdict") == "pass"
    assert (project / "qa" / "changes" / "CH-1" / "registry" / "skill-registry-check.json").is_file()


def test_attached_gate_does_not_reuse_acceptance_from_an_old_root(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change_dir = project / "qa" / "changes" / "CH-1"
    review = change_dir / "review" / "leaf.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({"decision": "needs_human_review"}), encoding="utf-8")
    digest = sha256(review.read_bytes()).hexdigest()
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "old-root",
            "checkpoint_ns": "old-root",
            "interrupt_id": "old-interrupt",
            "node_id": "human-review",
            "checkpoint": "leaf-review-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": digest},
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "old-root",
            "checkpoint_ns": "old-root",
            "interrupt_id": "old-interrupt",
            "action": "accept_risk",
            "reason": "accepted only in the old run",
            "who": "reviewer",
            "audited_reads_sha256": {"review/leaf.json": digest},
        },
    )
    compiled, contracts = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            review:
              uses: operation:no-op
              gate: leaf-review-gate
              retry: never
              timeout: local
          edges:
            - {from: START, to: review}
          routes:
            - from: review
              select: "node('review').gate.verdict"
              cases: {pass: END}
              default: STOP
        """
    )

    result = _runtime(project, compiled, contracts).run(compiled, "full", _ctx(project))

    assert result.status.status == "stopped"
    review_success = next(
        event for event in read_events_strict(change_dir) if event.get("type") == "task_attempt_succeeded"
    )
    gate_report = review_success.get("gate_report")
    assert isinstance(gate_report, dict)
    assert gate_report.get("verdict") == "needs_human_review"


def test_child_graph_stop_propagates_as_parent_business_stop(tmp_path: Path) -> None:
    """Child STOP must not become parent subgraph success (no explore after stop)."""
    project = _make_project(tmp_path, skills=True)
    compiled, contracts = _compile(
        """
        main:
          max_supersteps: 8
          nodes:
            bootstrap:
              uses: graph:bootstrap
              retry: never
              timeout: local
            explore:
              uses: operation:explore-marker
              outputs: [change:explore/advisory.json]
              retry: never
              timeout: local
          edges:
            - {from: START, to: bootstrap}
            - {from: bootstrap, to: explore}
            - {from: explore, to: END}
        bootstrap:
          max_supersteps: 5
          nodes:
            registry:
              uses: operation:stop
              with: {reason: "registry unavailable"}
              retry: never
              timeout: local
          edges:
            - {from: START, to: registry}
            - {from: registry, to: END}
        """
    )
    runtime = _runtime(project, compiled, contracts)
    result = runtime.run(compiled, "full", _ctx(project))
    assert result.status.status == "stopped", result.reason
    assert result.exit_code == 20

    events = read_events_strict(project / "qa" / "changes" / "CH-1")
    types = [e.get("type") for e in events]
    assert "graph_stopped" in types
    bootstrap_attempts = {
        e["attempt_id"]
        for e in events
        if e.get("type") == "task_attempt_started" and e.get("node_id") == "bootstrap"
    }
    assert bootstrap_attempts
    parent_success = [
        e
        for e in events
        if e.get("type") == "task_attempt_succeeded" and e.get("attempt_id") in bootstrap_attempts
    ]
    assert parent_success == []
    assert not (project / "qa" / "changes" / "CH-1" / "explore" / "advisory.json").exists()
