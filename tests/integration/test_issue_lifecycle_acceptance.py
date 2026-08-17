"""Hard acceptance tests for the Issue lifecycle workflow (Task 17).

Uses GraphRuntime with real issue operations, scripted AgentInvoker adapters,
and deterministic vue-fastapi-admin fixtures under tests/fixtures/issues/.

Twelve invariants (plan Task 17 Step 2):
  1. every abnormal Observation has valid evidence
  2. exact-fingerprint duplicate Problem count is zero
  3. Ledger replay bytes are identical
  4. Observation loss through failure/retry/crash is zero
  5. archive blocks caused by Issue state are zero
  6. unauthorized resolved/not-an-issue/accepted-risk transitions are zero
  7. initial and healing batches both reconcile before report
  8. linked fix plus complete authoritative verification resolves
  9. missing verification scope remains verification_pending
 10. post-archive review leaves archive bytes unchanged
 11. execution final_status is identical before and after Issue processing
 12. run_tests: false creates no Issue subgraph artifacts
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    ObservationDocument,
    Problem,
    ProblemProjection,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    load_execution_contracts,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.join import JoinHandler
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.issues.events import read_change_issue_events, read_problem_events
from assurance_agent.workflow.issues.projection import (
    dump_projection,
    project_change_issues,
    project_problems,
)
from assurance_agent.workflow.issues.review import (
    ReviewValidationError,
    build_problem_review_context,
    validate_review_action,
)
from assurance_agent.workflow.issues.transitions import (
    InvalidTransitionError,
    ResolutionContext,
    plan_resolution,
    validate_human_transition,
)
from tests.helpers_aa import write_aa_config

FIXTURE_ROOT = Path("tests/fixtures/issues/vue_fastapi_admin")
CHANGE_ID = "CH-VFA-ACCEPT"
INITIAL_BATCH = "20260725-120000"
HEALING_BATCH = "20260725-130000"
T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

# Shared fingerprint for dept HTTP 500 (API + fuzz evidence).
_DEPT_500_SURFACE = "POST /api/v1/dept"
_DEPT_500_SYMPTOM = "http_500_internal_server_error"


_ACCEPTANCE_WORKFLOW = """\
name: issue-lifecycle-acceptance
params:
  run_mode: {type: enum, values: [full], default: full}
  run_tests: {type: bool, default: true}
  problem_id: {type: str, default: ""}
  review_id: {type: str, default: ""}
entrypoints:
  full:
    graph: main
    allow: "params.run_mode == 'full'"
    restart: once
  issue-analyze:
    graph: issue-analyze
    restart: repeatable
  issue-review:
    graph: issue-review
    allow: "params.problem_id != '' and params.review_id != ''"
    restart: repeatable
  archive:
    graph: archive
    restart: once
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
    local: {run_seconds: 120, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 20
    nodes:
      seed-execution:
        uses: operation:seed-execution-batch
        retry: never
        timeout: local
      inspect-with-issues:
        uses: graph:inspect-with-issues
      capture-baseline:
        uses: operation:capture-issue-baseline
        retry: never
        timeout: local
      seed-healing:
        uses: operation:seed-healing-batch
        retry: never
        timeout: local
      inspect-healing:
        uses: graph:inspect-with-issues
      report-marker:
        uses: operation:report-gate-marker
        retry: never
        timeout: local
      skip-no-tests:
        uses: operation:no-op-marker
        retry: never
        timeout: local
    edges:
      - {from: START, to: seed-execution, when: "params.run_tests == true"}
      - {from: seed-execution, to: inspect-with-issues}
      - {from: inspect-with-issues, to: capture-baseline}
      - {from: capture-baseline, to: seed-healing}
      - {from: seed-healing, to: inspect-healing}
      - {from: inspect-healing, to: report-marker}
      - {from: report-marker, to: END}
      - {from: START, to: skip-no-tests, when: "params.run_tests == false"}
      - {from: skip-no-tests, to: END}

  inspect-with-issues:
    max_supersteps: 20
    nodes:
      fake-inspect:
        uses: operation:fake-inspect-gate
        retry: never
        timeout: local
      collect-observations:
        uses: operation:collect-observations
        retry: never
        timeout: local
      analyze-issues:
        uses: skill:aa-issue-analyzer
        outputs:
          - change:inspect/issue-candidates.json
          - change:inspect/issue-analysis-status.json
        retry: llm-default
        timeout: local
        recover:
          errors: [timeout, transport, rate_limit, invalid_output]
          via: record-analysis-failure
          continue_to: inspect-complete
      record-empty-analysis:
        uses: operation:record-empty-issue-analysis
        retry: never
        timeout: local
      reconcile-issues:
        uses: operation:reconcile-issues
        retry: project-sync
        timeout: local
        recover:
          errors: [conflict, transport]
          via: record-project-sync-pending
          continue_to: inspect-complete
      record-analysis-failure:
        uses: operation:record-issue-analysis-failure
        retry: never
        timeout: local
      record-project-sync-pending:
        uses: operation:record-project-sync-pending
        retry: never
        timeout: local
      inspect-complete:
        uses: operation:no-op-marker
        retry: never
        timeout: local
    edges:
      - {from: START, to: fake-inspect}
      - {from: fake-inspect, to: collect-observations}
      - {from: collect-observations, to: analyze-issues,
         when: "node('collect-observations').value.abnormal_count > 0"}
      - {from: collect-observations, to: record-empty-analysis,
         when: "node('collect-observations').value.abnormal_count == 0"}
      - {from: record-empty-analysis, to: reconcile-issues}
      - {from: analyze-issues, to: reconcile-issues}
      - {from: reconcile-issues, to: inspect-complete}
      - {from: inspect-complete, to: END}

  issue-analyze:
    max_supersteps: 10
    nodes:
      analyze-issues:
        uses: skill:aa-issue-analyzer
        outputs:
          - change:inspect/issue-candidates.json
          - change:inspect/issue-analysis-status.json
        retry: llm-default
        timeout: local
        recover:
          errors: [timeout, transport, rate_limit, invalid_output]
          via: record-analysis-failure
          continue_to: END
      record-analysis-failure:
        uses: operation:record-issue-analysis-failure
        retry: never
        timeout: local
      reconcile-issues:
        uses: operation:reconcile-issues
        retry: project-sync
        timeout: local
        recover:
          errors: [conflict, transport]
          via: record-project-sync-pending
          continue_to: END
      record-project-sync-pending:
        uses: operation:record-project-sync-pending
        retry: never
        timeout: local
    edges:
      - {from: START, to: analyze-issues}
      - {from: analyze-issues, to: reconcile-issues}
      - {from: reconcile-issues, to: END}

  issue-review:
    max_supersteps: 8
    nodes:
      load-problem:
        uses: operation:load-problem-review-context
        retry: never
        timeout: local
      human-interrupt:
        uses: builtin:interrupt
        interrupt:
          reason: "Review problem"
          checkpoint: review
          bind: audited_gate_read
          actions: [confirm_assessment, mark_not_an_issue, accept_risk, start_work, submit_resolution, stop]
      apply-review:
        uses: operation:apply-problem-review
        retry: project-sync
        timeout: local
    edges:
      - {from: START, to: load-problem}
      - {from: load-problem, to: human-interrupt}
      - {from: apply-review, to: END}
    routes:
      - from: human-interrupt
        select: "resume.action"
        cases:
          confirm_assessment: apply-review
          mark_not_an_issue: apply-review
          accept_risk: apply-review
          start_work: apply-review
          submit_resolution: apply-review
          stop: STOP
        default: STOP

  archive:
    max_supersteps: 4
    nodes:
      archive-copy:
        uses: operation:fake-archive
        retry: never
        timeout: local
    edges:
      - {from: START, to: archive-copy}
      - {from: archive-copy, to: END}
gates: {}
"""

_SUPPLEMENTAL_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:seed-execution-batch:
    handler: operation
    reads: []
    writes: ["change:execution/**"]
    authorization_writes: ["change:execution/**"]
    retryable_errors: []

  operation:seed-healing-batch:
    handler: operation
    reads: []
    writes: ["change:execution/**"]
    authorization_writes: ["change:execution/**"]
    retryable_errors: []

  operation:fake-inspect-gate:
    handler: operation
    reads: ["change:execution/**"]
    writes: ["change:inspect/failure-analysis.json", "change:inspect/quality-gate-result.json"]
    authorization_writes: ["change:inspect/failure-analysis.json", "change:inspect/quality-gate-result.json"]
    retryable_errors: []

  operation:report-gate-marker:
    handler: operation
    reads: ["change:inspect/issue-reconcile-status.json"]
    writes: ["change:report/acceptance-marker.json"]
    authorization_writes: ["change:report/acceptance-marker.json"]
    retryable_errors: []

  operation:no-op-marker:
    handler: operation
    side_effect_free: true

  operation:capture-issue-baseline:
    handler: operation
    reads: ["change:inspect/observations.json", "change:inspect/issue-evidence-manifest.json", "change:issues/snapshot.json"]
    writes: ["change:issues/baseline/**"]
    authorization_writes: ["change:issues/baseline/**"]
    retryable_errors: []

  operation:fake-archive:
    handler: operation
    reads: ["change:issues/**"]
    writes: ["project:qa/archive/**"]
    authorization_writes: ["project:qa/archive/**"]
    retryable_errors: []
"""


class FixedClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self._mono += seconds


@dataclass
class AcceptanceState:
    """Mutable scenario state shared across scripted handlers."""

    analyzer_mode: str = "success"
    analyzer_calls: int = 0
    reconcile_markers: list[str] = field(default_factory=list)
    execution_final_status: str = "FAIL"
    current_batch: str = INITIAL_BATCH
    baseline_observation_count: int = 0


def _sha256_dir(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode("utf-8"))
            digest.update(file.read_bytes())
    return digest.hexdigest()


def _copy_batch_fixture(change_dir: Path, batch_name: str, *, change_id: str) -> str:
    src = FIXTURE_ROOT / batch_name
    manifest = json.loads((src / "execution-manifest.json").read_text(encoding="utf-8"))
    batch_id = str(manifest["batch_id"])
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)

    manifest_out = dict(manifest)
    manifest_out["change_id"] = change_id
    (execution_dir / "execution-manifest.json").write_text(json.dumps(manifest_out), encoding="utf-8")

    runs_dir = execution_dir / "runs" / batch_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("api-result.json", "fuzz-result.json"):
        src_file = src / name
        if src_file.is_file():
            data = json.loads(src_file.read_text(encoding="utf-8"))
            data["change_id"] = change_id
            data["batch_id"] = batch_id
            (runs_dir / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    raw_dir = runs_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for log_name in ("api.log", "fuzz.log"):
        (raw_dir / log_name).write_text(f"fixture log for {batch_id}\n", encoding="utf-8")
    return batch_id


def _candidate_for_observation(obs: dict[str, Any], *, candidate_id: str) -> dict[str, Any]:
    case_id = obs.get("case_id") or ""
    kind = obs.get("kind")
    if case_id == "TC_TEST_BUG_001":
        classification = "test_bug"
        title = "Dept list assertion typo in generated test"
        surface = "tests/api/test_dept_api.py"
        surface_kind = "test"
        symptom = "assertion_typo_wrong_field"
    elif kind == "workaround":
        classification = "product_bug"
        title = "Dept empty name workaround still documents product defect"
        surface = _DEPT_500_SURFACE
        surface_kind = "endpoint"
        symptom = _DEPT_500_SYMPTOM
    else:
        classification = "product_bug"
        title = "Dept create returns HTTP 500 for empty name"
        surface = _DEPT_500_SURFACE
        surface_kind = "endpoint"
        symptom = _DEPT_500_SYMPTOM

    return {
        "candidate_id": candidate_id,
        "observation_ids": [obs["observation_id"]],
        "proposed": {
            "title": title,
            "classification": classification,
            "severity": "high" if classification == "product_bug" else "medium",
            "root_cause_hypothesis": "Backend validation missing for empty department name",
        },
        "affected_surface": {"kind": surface_kind, "value": surface},
        "fingerprint_inputs": {
            "surface": surface.lower() if surface_kind == "endpoint" else surface,
            "symptom": symptom,
            "qualifiers": None,
        },
        "possible_problem_ids": [],
        "confidence": 0.9,
        "recommended_action": "confirm and track",
    }


class ScriptedIssueAnalyzer:
    """Writes issue-candidates from live observations; can simulate timeout."""

    def __init__(self, state: AcceptanceState) -> None:
        self.state = state
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        self.state.analyzer_calls += 1
        if self.state.analyzer_mode == "timeout":
            return AgentResult(ok=False, error_kind="timeout", error="scripted timeout")
        if request.target != "skill:aa-issue-analyzer":
            return AgentResult(ok=False, error_kind="internal", error=f"unexpected target {request.target}")

        workspace = Path(request.workspace_root)
        change_root = workspace / "qa" / "changes" / request.change_id
        obs_path = change_root / "inspect" / "observations.json"
        manifest_path = change_root / "inspect" / "issue-evidence-manifest.json"
        if not obs_path.is_file() or not manifest_path.is_file():
            return AgentResult(ok=False, error_kind="invalid_output", error="missing observations")

        obs_doc = json.loads(obs_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        observations = obs_doc.get("observations") or []
        candidates = [
            _candidate_for_observation(obs, candidate_id=f"CAND-{idx:03d}")
            for idx, obs in enumerate(observations, start=1)
        ]
        candidates_doc = {
            "schema_version": "1.0",
            "change_id": obs_doc.get("change_id", CHANGE_ID),
            "batch_id": manifest.get("batch_id", self.state.current_batch),
            "evidence_bundle_digest": manifest.get("digest", ""),
            "candidates": candidates,
        }
        candidates_bytes = (
            json.dumps(candidates_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        candidate_digest = "sha256:" + hashlib.sha256(candidates_bytes).hexdigest()

        inspect_dir = change_root / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        (inspect_dir / "issue-candidates.json").write_bytes(candidates_bytes)
        status_doc = {
            "schema_version": "1.0",
            "change_id": candidates_doc["change_id"],
            "batch_id": candidates_doc["batch_id"],
            "status": "completed",
            "evidence_bundle_digest": candidates_doc["evidence_bundle_digest"],
            "candidate_count": len(candidates),
            "candidate_digest": candidate_digest,
        }
        (inspect_dir / "issue-analysis-status.json").write_text(
            json.dumps(status_doc, indent=2) + "\n", encoding="utf-8"
        )
        return AgentResult(ok=True)


class CombinedScriptedInvoker:
    def __init__(self, state: AcceptanceState) -> None:
        self._analyzer = ScriptedIssueAnalyzer(state)

    def invoke(self, request: AgentRequest) -> AgentResult:
        return self._analyzer.invoke(request)


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    (project / "qa" / "issues").mkdir(parents=True)
    write_aa_config(project)
    return project


def _context(
    project: Path,
    *,
    params: dict[str, object] | None = None,
) -> RuntimeContext:
    change = project / "qa" / "changes" / CHANGE_ID
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id=CHANGE_ID,
        params=params or {"run_mode": "full", "run_tests": True},
    )


def _compile_acceptance() -> tuple[CompiledWorkflow, ExecutionContractCatalog]:
    packaged = load_execution_contracts(Path("."))
    supplemental = parse_execution_contracts(_SUPPLEMENTAL_CONTRACTS)
    contracts = ExecutionContractCatalog(
        contracts={**packaged.contracts, **supplemental.contracts},
    )
    compiled = compile_workflow(parse_workflow_v2(_ACCEPTANCE_WORKFLOW), contracts)
    return compiled, contracts


def _custom_operations(state: AcceptanceState) -> dict[str, OperationFn]:
    ops = default_operations()

    def seed_execution_batch(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        batch_id = _copy_batch_fixture(workspace.change_dir, "initial", change_id=context.change_id)
        state.current_batch = batch_id
        return TaskResult(status="succeeded", value={"batch_id": batch_id})

    def seed_healing_batch(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        batch_id = _copy_batch_fixture(workspace.change_dir, "healing", change_id=context.change_id)
        state.current_batch = batch_id
        return TaskResult(status="succeeded", value={"batch_id": batch_id})

    def fake_inspect_gate(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        batch_id = state.current_batch
        (inspect_dir / "failure-analysis.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "change_id": context.change_id,
                    "source_manifest": "execution/execution-manifest.json",
                    "inspection_status": "completed",
                    "batch_id": batch_id,
                    "source_batch_id": batch_id,
                    "final_status": state.execution_final_status,
                    "inspect_mode": "primary",
                    "classification_performed": True,
                    "status": "analyzed",
                    "failures": [],
                    "hard_fails": [],
                    "needs_review": [],
                    "known_product_issues": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (inspect_dir / "quality-gate-result.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "change_id": context.change_id,
                    "batch_id": batch_id,
                    "dimensions": {
                        "functional": {
                            "status": state.execution_final_status,
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
                    "final_status": state.execution_final_status,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return TaskResult(status="succeeded", value={"final_status": state.execution_final_status})

    def report_gate_marker(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        marker = workspace.change_dir / "report" / "acceptance-marker.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"reached": True, "after_reconcile": True}) + "\n", encoding="utf-8")
        return TaskResult(status="succeeded", value={"marker": True})

    def no_op_marker(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="succeeded")

    def capture_issue_baseline(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        baseline_dir = workspace.change_dir / "issues" / "baseline"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        for name in ("observations.json", "issue-evidence-manifest.json"):
            src = workspace.change_dir / "inspect" / name
            if src.is_file():
                shutil.copy2(src, baseline_dir / name)
        snap = workspace.change_dir / "issues" / "snapshot.json"
        if snap.is_file():
            shutil.copy2(snap, baseline_dir / "snapshot.json")
        obs_doc = json.loads((baseline_dir / "observations.json").read_text(encoding="utf-8"))
        state.baseline_observation_count = len(obs_doc.get("observations") or [])
        return TaskResult(
            status="succeeded",
            value={"baseline_observation_count": state.baseline_observation_count},
        )

    def fake_archive(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        archive_dir = workspace.project_root / "qa" / "archive" / context.change_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "archive-summary.md").write_text("# archived\n", encoding="utf-8")
        issues_src = workspace.change_dir / "issues"
        if issues_src.is_dir():
            shutil.copytree(issues_src, archive_dir / "issues", dirs_exist_ok=True)
        return TaskResult(status="succeeded")

    original_reconcile = ops["operation:reconcile-issues"]

    def tracking_reconcile(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        result = original_reconcile(task, workspace, context)
        if result.status == "succeeded":
            state.reconcile_markers.append(state.current_batch)
        return result

    ops.update(
        {
            "operation:seed-execution-batch": seed_execution_batch,
            "operation:seed-healing-batch": seed_healing_batch,
            "operation:fake-inspect-gate": fake_inspect_gate,
            "operation:report-gate-marker": report_gate_marker,
            "operation:no-op-marker": no_op_marker,
            "operation:capture-issue-baseline": capture_issue_baseline,
            "operation:fake-archive": fake_archive,
            "operation:reconcile-issues": tracking_reconcile,
        }
    )
    return ops


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    *,
    state: AcceptanceState,
    clock: FixedClock | None = None,
) -> GraphRuntime:
    clock = clock or FixedClock()
    invoker = CombinedScriptedInvoker(state)

    def build(store, run_child):  # type: ignore[no-untyped-def]
        custom_ops = _custom_operations(state)
        op_handler = OperationHandler(custom_ops)
        agent = AgentHandler(invoker, store, contracts=contracts, compiled=compiled)
        handlers: dict[str, Any] = {
            "builtin:join": JoinHandler(),
            "builtin:gate": GateHandler(compiled),
            "builtin:interrupt": InterruptHandler(compiled),
            **{target: op_handler for target in custom_ops},
        }
        return HandlerNodeRunner(
            handlers,
            namespace_handlers={
                "skill": agent,
                "graph": SubgraphHandler(run_child),
            },
            compiled=compiled,
            object_store=store,
        )

    return assemble_graph_runtime(
        project_root=project,
        change_dir=project / "qa" / "changes" / CHANGE_ID,
        compiled=compiled,
        contracts=contracts,
        build_node_runner=build,
        clock=clock,
    )


def _load_snapshot(change_dir: Path) -> dict[str, Any]:
    return json.loads((change_dir / "issues" / "snapshot.json").read_text(encoding="utf-8"))


def _load_problems(project: Path) -> ProblemProjection:
    data = json.loads((project / "qa" / "issues" / "problems.json").read_text(encoding="utf-8"))
    return ProblemProjection.model_validate(data)


def _assert_observations_have_valid_evidence(baseline_dir: Path) -> None:
    obs_doc = ObservationDocument.model_validate(
        json.loads((baseline_dir / "observations.json").read_text(encoding="utf-8"))
    )
    manifest = json.loads((baseline_dir / "issue-evidence-manifest.json").read_text(encoding="utf-8"))
    allowed_paths = {entry["path"] for entry in manifest.get("entries", [])}
    for obs in obs_doc.observations:
        assert obs.evidence_refs, f"{obs.observation_id} missing evidence_refs"
        for ref in obs.evidence_refs:
            assert ref in allowed_paths or any(ref.endswith(p) for p in allowed_paths), (
                f"{obs.observation_id} evidence ref {ref!r} not in manifest"
            )


def _count_duplicate_fingerprint_problems(project: Path) -> int:
    problems = _load_problems(project)
    seen: set[str] = set()
    duplicates = 0
    for problem in problems.problems:
        digest = problem.fingerprint.digest
        if digest in seen:
            duplicates += 1
        seen.add(digest)
    return duplicates


def _replay_bytes_identical(change_dir: Path, project: Path) -> None:
    """Projections on disk must be canonical and deterministically re-dumpable."""
    snap_path = change_dir / "issues" / "snapshot.json"
    prob_path = project / "qa" / "issues" / "problems.json"
    snap_bytes = snap_path.read_bytes()
    prob_bytes = prob_path.read_bytes()

    snap_model = ChangeIssueSnapshot.model_validate(json.loads(snap_bytes.decode("utf-8")))
    prob_model = ProblemProjection.model_validate(json.loads(prob_bytes.decode("utf-8")))

    assert dump_projection(snap_model) == snap_bytes
    assert dump_projection(prob_model) == prob_bytes
    assert dump_projection(snap_model) == dump_projection(snap_model)
    assert dump_projection(prob_model) == dump_projection(prob_model)

    events_path = change_dir / "issues" / "events.jsonl"
    if events_path.is_file() and events_path.read_text().strip():
        change_events = read_change_issue_events(events_path)
        replayed_snap = dump_projection(project_change_issues(change_events))
        assert replayed_snap == snap_bytes

    project_events_path = project / "qa" / "issues" / "events.jsonl"
    if project_events_path.is_file() and project_events_path.read_text().strip():
        problem_events = read_problem_events(project_events_path)
        replayed_prob = dump_projection(project_problems(problem_events))
        assert replayed_prob == prob_bytes


def _count_unauthorized_transition_successes(problems: list[Problem]) -> int:
    """Return count of illegal transitions that incorrectly succeeded (must be 0)."""
    successes = 0
    illegal_attempts: list[tuple[str, Problem]] = [
        ("mark_not_an_issue", _problem_with_status("resolved")),
        ("accept_risk", _problem_with_status("resolved")),
        ("start_work", _problem_with_status("not_an_issue")),
        ("confirm_assessment", _problem_with_status("accepted_risk")),
    ]
    for action, problem in illegal_attempts:
        try:
            validate_human_transition(
                problem,
                action,
                expected_problem_version=problem.version,
                reason="illegal attempt",
                evidence_refs=["OCC-test"],
                payload={"status": "resolved"},
            )
            successes += 1
        except (InvalidTransitionError, ReviewValidationError):
            pass
    detected = _problem_with_status("detected")
    try:
        validate_human_transition(
            detected,
            "confirm_assessment",
            expected_problem_version=1,
            reason="bad payload",
            evidence_refs=["OCC-test"],
            payload={"status": "resolved", "authority": "human_confirmed"},
        )
        successes += 1
    except InvalidTransitionError:
        pass
    return successes


def _problem_with_status(status: str) -> Problem:
    payload: dict[str, object] = {
        "problem_id": "PROB-test000000000001",
        "fingerprint": {"version": "1", "digest": "a" * 64},
        "title": "test",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
        },
        "status": status,
        "first_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-test000000000001"},
        "last_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-test000000000001"},
        "occurrences": ["OCC-test000000000001"],
        "version": 1,
    }
    if status == "resolved":
        payload["resolution"] = {
            "resolved_at": "2026-07-25T12:00:00Z",
            "change_id": CHANGE_ID,
            "batch_id": HEALING_BATCH,
            "disposition": "fixed",
            "verification_scope": ["TC_TEST"],
            "evidence_digest": "sha256:" + "d" * 64,
        }
    return Problem.model_validate(payload)


def test_issue_lifecycle_acceptance_invariants(tmp_path: Path) -> None:
    """Run the vue-fastapi-admin fixture through the acceptance workflow."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile_acceptance()
    state = AcceptanceState(execution_final_status="FAIL")
    runtime = _build_runtime(project, compiled, contracts, state=state)
    change_dir = project / "qa" / "changes" / CHANGE_ID

    pre_final_status = state.execution_final_status

    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 0, result.reason

    # 1. every abnormal Observation has valid evidence
    baseline_dir = change_dir / "issues" / "baseline"
    _assert_observations_have_valid_evidence(baseline_dir)

    # 2. exact-fingerprint duplicate Problem count is zero
    assert _count_duplicate_fingerprint_problems(project) == 0

    # 3. Ledger replay bytes are identical
    _replay_bytes_identical(change_dir, project)

    obs_before_failure_retry = state.baseline_observation_count
    assert obs_before_failure_retry >= 4, "expected dept 500, workaround, test_bug, fuzz signals"

    # 4. Observation loss through failure/retry — simulate analyzer timeout then retry
    baseline_obs_bytes = (baseline_dir / "observations.json").read_bytes()
    state.analyzer_mode = "timeout"
    analyze_result = runtime.run(compiled, "issue-analyze", _context(project))
    assert analyze_result.exit_code == 0
    assert (baseline_dir / "observations.json").read_bytes() == baseline_obs_bytes

    state.analyzer_mode = "success"
    retry_result = runtime.run(compiled, "issue-analyze", _context(project))
    assert retry_result.exit_code == 0
    assert (baseline_dir / "observations.json").read_bytes() == baseline_obs_bytes
    assert state.baseline_observation_count == obs_before_failure_retry

    # 5. archive blocks caused by Issue state are zero
    archive_result = runtime.run(compiled, "archive", _context(project))
    assert archive_result.exit_code == 0, archive_result.reason

    # 6. unauthorized resolved/not-an-issue/accepted-risk transitions are zero
    problems = _load_problems(project)
    assert _count_unauthorized_transition_successes(list(problems.problems)) == 0

    # 7. initial and healing batches both reconcile before report
    assert INITIAL_BATCH in state.reconcile_markers
    assert HEALING_BATCH in state.reconcile_markers
    assert (change_dir / "report" / "acceptance-marker.json").is_file()

    # 8. linked fix plus complete authoritative verification resolves
    product_probs = [p for p in problems.problems if p.assessment.classification == "product_bug"]
    assert product_probs, "expected at least one product_bug Problem"
    target = product_probs[0]
    triaged = target.model_copy(
        update={
            "status": "triaged",
            "version": target.version + 1,
            "assessment": target.assessment.model_copy(update={"authority": "human_confirmed"}),
        }
    )
    triaged_projection = ProblemProjection(
        schema_version="1.0",
        generated_at=problems.generated_at,
        problems=[triaged if p.problem_id == target.problem_id else p for p in problems.problems],
    )
    ctx = build_problem_review_context(triaged.problem_id, triaged_projection)
    verify_events = validate_review_action(
        ctx,
        "submit_resolution",
        {
            "verification_scope": ["TC_DEPT_NEG_001", "TC_FUZZ_DEPT_500"],
            "linked_fix_disposition": "healing-apply-dept-validation",
            "change_id": CHANGE_ID,
            "batch_id": HEALING_BATCH,
        },
        "verification requested after healing fix",
        "qa-lead",
        projection=triaged_projection,
    )
    assert verify_events[0].type == "problem_verification_requested"
    pending = triaged.model_copy(update={"status": "verification_pending", "version": triaged.version + 1})
    resolution = plan_resolution(
        pending,
        expected_problem_version=pending.version,
        linked_fix_disposition="healing-apply-dept-validation",
        verification_scope=["TC_DEPT_NEG_001", "TC_FUZZ_DEPT_500"],
        verification_evidence_digest="sha256:" + "b" * 64,
        change_id=CHANGE_ID,
        batch_id=HEALING_BATCH,
        context=ResolutionContext(
            linked_fix_exists=True,
            authoritative_batch_selected_target=True,
            verification_cases_executed=True,
            verification_cases_passed=True,
            fingerprint_absent_in_batch=True,
        ),
    )
    assert resolution.event_type == "problem_resolved"
    assert resolution.payload["next_status"] == "resolved"

    # 9. missing verification scope remains verification_pending
    test_bug = next(p for p in problems.problems if p.assessment.classification == "test_bug")
    triaged_bug = test_bug.model_copy(
        update={
            "status": "triaged",
            "version": test_bug.version + 1,
            "assessment": test_bug.assessment.model_copy(update={"authority": "human_confirmed"}),
        }
    )
    bug_projection = ProblemProjection(
        schema_version="1.0",
        generated_at=problems.generated_at,
        problems=[triaged_bug if p.problem_id == test_bug.problem_id else p for p in problems.problems],
    )
    ctx_bug = build_problem_review_context(triaged_bug.problem_id, bug_projection)
    bug_verify = validate_review_action(
        ctx_bug,
        "submit_resolution",
        {
            "verification_scope": ["TC_TEST_BUG_001"],
            "linked_fix_disposition": "test-fix-only",
            "change_id": CHANGE_ID,
            "batch_id": HEALING_BATCH,
        },
        "request verification for test bug",
        "qa-lead",
        projection=bug_projection,
    )
    assert bug_verify[0].type == "problem_verification_requested"
    bug_pending = triaged_bug.model_copy(
        update={"status": "verification_pending", "version": triaged_bug.version + 1}
    )
    with pytest.raises(InvalidTransitionError):
        plan_resolution(
            bug_pending,
            expected_problem_version=bug_pending.version,
            linked_fix_disposition="test-fix-only",
            verification_scope=["TC_TEST_BUG_001"],
            verification_evidence_digest="sha256:" + "c" * 64,
            change_id=CHANGE_ID,
            batch_id=HEALING_BATCH,
            context=ResolutionContext(
                linked_fix_exists=True,
                authoritative_batch_selected_target=True,
                verification_cases_executed=False,
                verification_cases_passed=False,
                fingerprint_absent_in_batch=False,
            ),
        )
    assert bug_pending.status == "verification_pending"

    # 10. post-archive review leaves archive bytes unchanged
    archive_issues = project / "qa" / "archive" / CHANGE_ID / "issues"
    digest_before = _sha256_dir(archive_issues)
    review_target = next(
        p for p in _load_problems(project).problems if p.assessment.classification == "test_bug"
    )
    review_projection = _load_problems(project)
    review_ctx = build_problem_review_context(review_target.problem_id, review_projection)
    review_events = validate_review_action(
        review_ctx,
        "mark_not_an_issue",
        {"evidence_refs": list(review_target.occurrences[:1])},
        "test assertion typo is not a product defect",
        "pm",
        projection=review_projection,
    )
    assert review_events[0].type == "problem_marked_not_an_issue"
    assert _sha256_dir(archive_issues) == digest_before

    # 11. execution final_status is identical before and after Issue processing
    post_gate = json.loads((change_dir / "inspect" / "quality-gate-result.json").read_text(encoding="utf-8"))
    assert post_gate.get("final_status") == pre_final_status == "FAIL"


def test_run_tests_false_creates_no_issue_subgraph_artifacts(tmp_path: Path) -> None:
    """Assertion 12: run_tests=false skips Issue subgraph entirely."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile_acceptance()
    state = AcceptanceState()
    runtime = _build_runtime(project, compiled, contracts, state=state)

    result = runtime.run(
        compiled,
        "full",
        _context(project, params={"run_mode": "full", "run_tests": False}),
    )
    assert result.exit_code == 0
    change_dir = project / "qa" / "changes" / CHANGE_ID
    assert not list(change_dir.rglob("observations.json"))
    assert not list(change_dir.rglob("issue-candidates.json"))
    assert not list(change_dir.rglob("issue-reconcile-status.json"))
    assert not list(change_dir.rglob("issues/events.jsonl"))


def test_acceptance_graph_orders_inspect_before_report_without_retro() -> None:
    """Initial + healing inspect-with-issues both precede report; no Retro/Improvement."""
    schema = parse_workflow_v2(_ACCEPTANCE_WORKFLOW)
    main = schema.graphs["main"]
    edges = {(edge.from_, edge.to) for edge in main.edges}
    assert ("seed-execution", "inspect-with-issues") in edges
    assert ("seed-healing", "inspect-healing") in edges
    assert ("inspect-healing", "report-marker") in edges
    uses = {node.uses for node in main.nodes.values()}
    assert main.nodes["inspect-with-issues"].uses == "graph:inspect-with-issues"
    assert main.nodes["inspect-healing"].uses == "graph:inspect-with-issues"
    assert "graph:inspect-with-issues" in uses
    for forbidden in (
        "graph:retro-workflow",
        "operation:reconcile-improvements",
        "operation:retro-collect",
        "skill:aa-retro",
        "operation:apply-improvement-review",
        "operation:export-knowledge-improvement",
    ):
        assert forbidden not in uses
