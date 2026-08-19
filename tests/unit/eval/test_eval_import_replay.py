"""Eval-layer proof: seeded fixture + import_checkpoint ledger semantics.

Exercises ``seed_change`` → ``execute_attempt`` → real ``GraphRuntime.import_checkpoint``
(not a stub that skips the ledger). Asserts import events, no physical attempts on
imported tasks, unimported-only resume, and write-scan evidence on project diffs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from tests.helpers_aa import write_aa_config

from assurance_agent.eval import write_scan
from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.fixtures import write_fixture_lock
from assurance_agent.eval.types import DatasetSample
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.runtime_factory import RuntimeBundle, assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:codegen:
    handler: operation
    side_effect_free: false
    writes: ["change:codegen/**"]
    authorization_writes: ["change:codegen/**"]
  builtin:gate:
    handler: builtin
    side_effect_free: true
"""

_SCHEMA = """\
name: eval-import-replay
params:
  run_mode: {type: enum, values: [full, codegen-only], default: full}
  test_types: {type: list, default: [api]}
  run_tests: {type: bool, default: false}
entrypoints:
  execute:
    graph: main
    allow: "params.run_mode == 'full' or params.run_mode == 'codegen-only'"
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 8
    nodes:
      review:
        uses: operation:no-op
        outputs: ["change:review/api-plan-review.json"]
        gate: api-plan-review-gate
        retry: never
        timeout: local
      codegen:
        uses: operation:codegen
        outputs: ["change:codegen/summary.md"]
        retry: never
        timeout: local
    edges:
      - {from: START, to: review}
      - {from: review, to: codegen}
      - {from: codegen, to: END}
gates:
  api-plan-review-gate:
    reads: [{path: review/api-plan-review.json, as: review}]
    missing_file_is: stop
    pass_when: "review.decision == 'pass'"
    needs_fix_when: "review.decision == 'needs_fix'"
"""


class _FakeInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        return AgentResult(ok=True)


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


def _ops() -> dict[str, OperationFn]:
    ops = default_operations()

    def codegen(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:  # noqa: ANN001, ARG001
        out = workspace.change_dir / "codegen" / "summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# codegen ok\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:codegen"] = codegen
    return ops


def _runtime_factory(*, write_forbidden: bool = False):
    def factory(*, project_root: Path, change_id: str, adapter: object) -> RuntimeBundle:  # noqa: ARG001
        change = project_root / "qa" / "changes" / change_id
        contracts = parse_execution_contracts(_CONTRACTS)
        compiled = compile_workflow(parse_workflow_v2(_SCHEMA), contracts)

        def build_node_runner(_store, run_child):  # noqa: ANN001, ANN202
            from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler

            handler = OperationHandler(_ops())
            targets: dict[str, object] = {name: handler for name in _ops()}
            subgraph = SubgraphHandler(run_child)
            for graph_id in compiled.graphs:
                targets[f"graph:{graph_id}"] = subgraph
            return HandlerNodeRunner(targets)  # type: ignore[arg-type]

        runtime = assemble_graph_runtime(
            project_root=project_root,
            change_dir=change,
            compiled=compiled,
            contracts=contracts,
            operations=_ops(),
            build_node_runner=build_node_runner,
        )

        if write_forbidden:

            class _Wrap:
                def import_checkpoint(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN201
                    result = runtime.import_checkpoint(*a, **k)
                    evil = project_root / "src" / "evil.py"
                    evil.parent.mkdir(parents=True, exist_ok=True)
                    evil.write_text("print('forbidden')\n", encoding="utf-8")
                    return result

                def status(self, invocation_id: str):  # noqa: ANN201
                    return runtime.status(invocation_id)

                def run(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN201
                    return runtime.run(*a, **k)

            return RuntimeBundle(runtime=_Wrap(), compiled=compiled)  # type: ignore[arg-type]

        return RuntimeBundle(runtime=runtime, compiled=compiled)

    return factory


def _seed_fixture_project(tmp_path: Path) -> tuple[Path, Path]:
    """Return (sut, fixtures_root) with tier imports for execute entrypoint."""
    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / ".aa" / "workflow-schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (sut / ".aa" / "execution-contracts.yaml").write_text(_CONTRACTS, encoding="utf-8")
    _git_init(sut)

    fixtures = sut / "eval-fixtures"
    sample = fixtures / "samples" / "eval-sample-001"
    sample.mkdir(parents=True)
    (sample / "proposal.md").write_text("# proposal\n", encoding="utf-8")
    (sample / "workflow-state.json").write_text(
        json.dumps(
            {
                "phases": {"skill_registry_check": {"status": "pass"}},
                "run_context": {
                    "interaction_mode": "autonomous",
                    "orchestrator_skill": "aa-workflow",
                },
            }
        ),
        encoding="utf-8",
    )
    review = sample / "review"
    review.mkdir()
    (review / "api-plan-review.json").write_text(
        json.dumps(
            {
                "decision": "pass",
                "codegen_readiness": "ready",
                "auto_fix_allowed": False,
                "human_review_required": False,
                "risk_level": "low",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (fixtures / "tiers").mkdir()
    (fixtures / "tiers" / "L2-codegen-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L2-codegen-seed",
                "paths": [
                    "proposal.md",
                    "workflow-state.json",
                    "review/api-plan-review.json",
                ],
                "imports": {
                    "execute": {
                        "entrypoint": "execute",
                        "inputs": ["change:proposal.md"],
                        "completed": [
                            {
                                "path": "main",
                                "graph": "main",
                                "node": "review",
                                "outputs": ["change:review/api-plan-review.json"],
                                "gate": "api-plan-review-gate",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_fixture_lock(fixtures, {"eval-sample-001": "samples/eval-sample-001"})
    return sut, fixtures


def _run_import_attempt(
    tmp_path: Path,
    *,
    run_mode: str = "codegen-only",
    write_forbidden: bool = False,
) -> tuple[Path, Path]:
    sut, fixtures = _seed_fixture_project(tmp_path)
    sample = DatasetSample(
        id="WIR-001",
        suite="workflow-api-codegen",
        input={
            "change_id": "eval-sample-001",
            "fixture_id": "eval-sample-001",
            "fixture_tier": "L2-codegen-seed",
        },
        expected={},
    )
    attempt = tmp_path / "run" / "WIR-001" / "attempt-0"
    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-api-codegen",
        sut_dir=sut,
        adapter=_FakeInvoker(),
        entrypoint="execute",
        runtime_factory=_runtime_factory(write_forbidden=write_forbidden),
        fixtures_root=fixtures,
        run_mode=run_mode,
        selected_layers=("api",),
        run_tests=False,
    )
    assert result.exit_code == 0, result.error
    change = sut / "qa" / "changes" / "eval-sample-001"
    return attempt, change


def test_eval_seed_import_writes_checkpoint_and_task_imported(tmp_path: Path) -> None:
    _attempt, change = _run_import_attempt(tmp_path)
    events = read_events_strict(change)
    types = [e["type"] for e in events]
    assert "checkpoint_imported" in types
    assert "task_imported" in types
    imported = [e for e in events if e["type"] == "task_imported"]
    assert any(e.get("node_id") == "review" for e in imported)


def test_eval_imported_tasks_have_no_physical_attempts(tmp_path: Path) -> None:
    _attempt, change = _run_import_attempt(tmp_path)
    events = read_events_strict(change)
    for ev in events:
        if ev.get("type") in {"task_attempt_started", "task_attempt_succeeded"}:
            assert ev.get("node_id") != "review", ev


def test_eval_codegen_only_starts_only_unimported_task(tmp_path: Path) -> None:
    _attempt, change = _run_import_attempt(tmp_path, run_mode="codegen-only")
    events = read_events_strict(change)
    started_nodes = {e.get("node_id") for e in events if e.get("type") == "task_attempt_started"}
    started_task_ids = {e.get("task_id") for e in events if e.get("type") == "task_attempt_started"}
    succeeded_task_ids = {e.get("task_id") for e in events if e.get("type") == "task_attempt_succeeded"}
    assert started_nodes == {"codegen"}
    assert started_task_ids and started_task_ids <= succeeded_task_ids
    assert "review" not in started_nodes
    assert any(e.get("type") == "task_imported" and e.get("node_id") == "review" for e in events)
    assert (change / "codegen" / "summary.md").is_file()


def test_eval_import_path_forbidden_write_reports_project_diff(tmp_path: Path) -> None:
    attempt, _change = _run_import_attempt(tmp_path, write_forbidden=True)
    diff_path = attempt / "evidence" / "write-diff.json"
    policy_path = attempt / "evidence" / "write-policy.json"
    assert diff_path.is_file()
    assert policy_path.is_file()
    # Task 17: write-diff.json is WriteDiffV1; forbidden count comes from policy scan.
    diff = write_scan.WriteDiffV1.model_validate_json(diff_path.read_bytes())
    policy = write_scan.WritePolicyV1.model_validate_json(policy_path.read_bytes())
    scan = write_scan.scan_forbidden_writes_from_diff(diff, policy)
    assert "src/evil.py" in scan.violation_paths
    assert scan.forbidden_write_executed_count >= 1
    assert "src/evil.py" in scan.changed_paths
    # Allowed change-dir artifact from the unimported codegen task still appears.
    assert any(p.startswith("qa/changes/") for p in scan.changed_paths)
