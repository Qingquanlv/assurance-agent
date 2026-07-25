"""End-to-end acceptance for Retro / Issue / Improvement clean-cut (Task 13).

Six scenarios from the approved design §12.5 plus packaged graph-order guards.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent import resources
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
    ImprovementVerification,
)
from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    Problem,
    ProblemProjection,
    ProblemResolution,
)
from assurance_agent.retro.accept_stage import run_retro_accept
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
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import ResourceClaims, load_execution_contracts
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import (
    RetryPolicyDef,
    TimeoutPolicyDef,
    parse_workflow_v2,
)
from assurance_agent.workflow.improvements.events import (
    IMPROVEMENT_EVENT_ADAPTER,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)
from assurance_agent.workflow.improvements.knowledge_delivery import (
    ImprovementDeliveryError,
    KnowledgeDeltaDelivery,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.memory_delivery import (
    MemoryPatchDelivery,
    sha256_bytes,
)
from assurance_agent.workflow.issues.operations import (
    collect_observations_operation,
    reconcile_issues_operation,
)
from assurance_agent.workflow.issues.projection import dump_projection
from tests.helpers_aa import write_aa_config

INTENT = "Preserve pytest E lines when classifying failures"
TARGET_WORKFLOW = "assurance_agent/workflow/inspect"
TARGET_MEMORY = ".aa/memory/aa-run.md"
_SCENARIO1_CHANGE_ID = "CH-S1-PRODUCT"
_SCENARIO1_BATCH_ID = "20260726-010000"


def _load_packaged_schema():
    text = resources.read_text("schemas", "workflow-schema.yaml")
    return parse_workflow_v2(text)


def _entrypoint_graph(schema, entry: str) -> str:
    ep = schema.entrypoints[entry]
    return ep.graph if hasattr(ep, "graph") else ep.graph_id


def _reachable_graphs(schema, entry: str) -> set[str]:
    start = _entrypoint_graph(schema, entry)
    seen: set[str] = set()
    stack = [start]
    while stack:
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        graph = schema.graphs[gid]
        for node in graph.nodes.values():
            if node.uses.startswith("graph:"):
                stack.append(node.uses.removeprefix("graph:"))
    return seen


def _edge_pairs(schema, graph_id: str) -> set[tuple[str, str]]:
    graph = schema.graphs[graph_id]
    return {(edge.from_, edge.to) for edge in graph.edges}


def _retro_context(
    retro_id: str,
    *,
    change_ids: tuple[str, ...] = ("CH-1",),
    evidence_ids: tuple[str, ...] = ("PROB-1",),
) -> RetroContext:
    return RetroContext(
        retro_id=retro_id,
        generated_at="2026-07-26T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=change_ids),
            change_ids=change_ids,
        ),
        source_manifest=RetroSourceManifest(
            issue_slice_sha256="sha256:slice",
            issue_sources=(
                RetroSourceDescriptor(
                    kind="change_issue_ledger",
                    change_id=change_ids[0],
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
        signal_count=1,
    )


def _candidate(
    *,
    candidate_id: str = "IMP-CAND-1",
    kind: ImprovementKind = ImprovementKind.WORKFLOW,
    delivery: DeliveryKind = DeliveryKind.CHANGE_DRAFT,
    problem_ids: tuple[str, ...] = ("PROB-1",),
    target: str = TARGET_WORKFLOW,
    proposed_change: str = INTENT,
    knowledge_delta: DataKnowledgeProposal | None = None,
) -> ImprovementCandidate:
    data: dict = {
        "candidate_id": candidate_id,
        "kind": kind,
        "delivery": delivery,
        "source_refs": ImprovementSourceRefs(problem_ids=problem_ids),
        "target": target,
        "rationale": "Repeated pattern across Changes",
        "proposed_change": proposed_change,
        "verification": ImprovementVerification(
            suites=("workflow-full",),
            success_criteria="Pattern no longer observed",
        ),
        "risk": "low",
        "confidence": "high",
    }
    if knowledge_delta is not None:
        data["knowledge_delta"] = knowledge_delta
    return ImprovementCandidate.model_validate(data)


def _write_retro_run(
    project: Path,
    *,
    retro_id: str,
    context: RetroContext,
    candidates: tuple[ImprovementCandidate, ...],
) -> Path:
    retro_dir = project / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "context.json").write_text(
        json.dumps(context.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    document = ImprovementCandidateDocument(
        retro_id=retro_id,
        context_sha256=context_sha256(context),
        candidates=candidates,
    )
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(document.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return retro_dir


def _problem(
    *,
    problem_id: str = "PROB-1",
    classification: str = "product_bug",
    status: str = "triaged",
    authority: str = "llm_provisional",
) -> Problem:
    payload: dict = {
        "problem_id": problem_id,
        "fingerprint": {"version": "1", "digest": hashlib.sha256(problem_id.encode()).hexdigest()},
        "title": f"Problem {problem_id}",
        "assessment": {
            "classification": classification,
            "severity": "high",
            "authority": authority,
        },
        "status": status,
        "first_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "version": 1,
    }
    if status == "resolved":
        payload["resolution"] = {
            "resolved_at": "2026-07-26T00:00:00Z",
            "change_id": "CH-1",
            "batch_id": "B-1",
            "disposition": "fixed",
            "verification_scope": ["api"],
            "evidence_digest": "e" * 64,
        }
    return Problem.model_validate(payload)


def _seed_problems(project: Path, *problems: Problem) -> bytes:
    issues = project / "qa" / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    projection = ProblemProjection(
        schema_version="1.0",
        generated_at="2026-07-26T00:00:00Z",
        problems=list(problems),
    )
    blob = dump_projection(projection)
    (issues / "problems.json").write_bytes(blob)
    (issues / "events.jsonl").write_text(
        '{"schema_version":"1.0","seq":1,"event_id":"EVT-1","idempotency_key":"IDEM-1",'
        '"ts":"2026-07-26T00:00:00Z","type":"problem_detected","problem_id":'
        f'"{problems[0].problem_id}","expected_problem_version":0}}\n',
        encoding="utf-8",
    )
    return blob


def _seed_l1(project: Path) -> bytes:
    path = project / ".aa" / "data-knowledge.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "version: 1\n"
        "accounts: {}\n"
        "auth: {}\n"
        "entities: {}\n"
        "capabilities:\n"
        "  domain_factories: {}\n"
        "  adapters: {}\n"
        "  cleanup: {}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def _passing_runner():
    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        run_id = "eval-accept-1"
        run = Path(sut_dir) / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True, exist_ok=True)
        (run / "metrics.json").write_text(
            json.dumps({"run_id": run_id, "suite": suite, "metrics": {"evidence_integrity": 1.0}}),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "verdict": "pass",
            "metrics": {"evidence_integrity": 1.0},
            "hard_gate_failures": [],
            "suite_contract": {
                "thresholds": [{"metric": "evidence_integrity", "gate": "hard", "op": "gte", "value": 0.95}]
            },
            "baseline_metrics": {"evidence_integrity": 1.0},
        }

    return eval_runner


class _IssueOpWorkspace:
    """Minimal workspace for direct issue operation invocation."""

    def __init__(self, change_dir: Path, project_root: Path) -> None:
        self.change_dir = change_dir
        self.project_root = project_root


def _issue_op_task(*, node_id: str, target: str) -> ExecutableTask:
    return ExecutableTask(
        task_id=f"task-{node_id}",
        invocation_id="inv-s1",
        checkpoint_ns="ns-s1",
        graph_id="inspect-with-issues",
        node_id=node_id,
        structural_path=node_id,
        input={},
        input_sha256="0" * 64,
        contract_digest="0" * 64,
        retryable_errors=("timeout", "transport", "conflict"),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=300, heartbeat_seconds=60),
        target=target,
        resources=ResourceClaims(),
    )


def _setup_failed_execution(change_dir: Path, *, change_id: str, batch_id: str) -> None:
    """Seed a minimal failed API batch so collect_observations emits abnormals."""
    execution_dir = change_dir / "execution"
    runs_dir = execution_dir / "runs" / batch_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    (execution_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "batch_id": batch_id,
                "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
                "result_files": {"api": f"runs/{batch_id}/api-result.json"},
            }
        ),
        encoding="utf-8",
    )
    (runs_dir / "api-result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "batch_id": batch_id,
                "target": "api",
                "status": "failed",
                "command": "pytest tests/api",
                "source": {"framework": "pytest", "raw_log": ""},
                "total": 1,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "cases": [
                    {
                        "case_id": "API-DEPT-NEG-001",
                        "status": "failed",
                        "file": "tests/api/test_dept.py",
                        "test_name": "test_empty_name_returns_500",
                        "duration_ms": 12,
                        "message": "HTTP 500 from POST /api/v1/dept",
                        "raw_log_ref": "",
                        "trace": "",
                        "screenshot": "",
                        "video": "",
                    }
                ],
                "unmapped_tests": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_product_bug_candidates(change_dir: Path, *, change_id: str) -> None:
    """Scripted analyze step: materialize product_bug candidates from live observations."""
    obs_doc = json.loads((change_dir / "inspect" / "observations.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (change_dir / "inspect" / "issue-evidence-manifest.json").read_text(encoding="utf-8")
    )
    observations = obs_doc.get("observations") or []
    assert observations, "collect_observations must emit at least one observation"
    candidates = [
        IssueCandidate(
            candidate_id=f"CAND-{idx:03d}",
            observation_ids=[obs["observation_id"]],
            proposed=IssueCandidateProposed(
                title="Dept create returns HTTP 500 for empty name",
                classification="product_bug",
                severity="high",
                root_cause_hypothesis="Backend validation missing for empty department name",
            ),
            affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept"),
            fingerprint_inputs=FingerprintInputs(
                surface="post /api/v1/dept",
                symptom="http_500_internal_server_error",
            ),
            possible_problem_ids=[],
            confidence=0.9,
            recommended_action="confirm and track",
        )
        for idx, obs in enumerate(observations, start=1)
    ]
    doc = IssueCandidateDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=str(manifest["batch_id"]),
        evidence_bundle_digest=str(manifest["digest"]),
        candidates=candidates,
    )
    inspect_dir = change_dir / "inspect"
    (inspect_dir / "issue-candidates.json").write_bytes(dump_projection(doc))
    candidate_digest = "sha256:" + hashlib.sha256(
        (inspect_dir / "issue-candidates.json").read_bytes()
    ).hexdigest()
    (inspect_dir / "issue-analysis-status.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "batch_id": manifest["batch_id"],
                "status": "completed",
                "evidence_bundle_digest": manifest["digest"],
                "candidate_count": len(candidates),
                "candidate_digest": candidate_digest,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _drive_issue_ops_create_product_bug(project: Path) -> list[Problem]:
    """Create a product_bug via real collect → analyze artifacts → reconcile ops.

    Chosen path (documented for Task 13): direct product issue operations that
    mirror inspect-with-issues, not a full agent-backed GraphRuntime run and not
    a hand-written problems.json seed. The packaged full-graph topology assert
    separately proves Retro/Improvement are unreachable from ``full``.
    """
    change_dir = project / "qa" / "changes" / _SCENARIO1_CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    (project / "qa" / "issues").mkdir(parents=True, exist_ok=True)
    _setup_failed_execution(
        change_dir, change_id=_SCENARIO1_CHANGE_ID, batch_id=_SCENARIO1_BATCH_ID
    )

    workspace = _IssueOpWorkspace(change_dir, project)
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        change_id=_SCENARIO1_CHANGE_ID,
        params={"run_mode": "full", "run_tests": True},
    )

    collect = collect_observations_operation(
        _issue_op_task(node_id="collect-observations", target="operation:collect-observations"),
        workspace,  # type: ignore[arg-type]
        context,
    )
    assert collect.status == "succeeded", collect.error
    assert isinstance(collect.value, dict)
    assert int(collect.value["abnormal_count"]) >= 1

    _write_product_bug_candidates(change_dir, change_id=_SCENARIO1_CHANGE_ID)

    reconcile = reconcile_issues_operation(
        _issue_op_task(node_id="reconcile-issues", target="operation:reconcile-issues"),
        workspace,  # type: ignore[arg-type]
        context,
    )
    assert reconcile.status == "succeeded", reconcile.error
    assert isinstance(reconcile.value, dict)
    assert reconcile.value["reconcile_status"] == "completed"

    projection = ProblemProjection.model_validate(
        json.loads((project / "qa" / "issues" / "problems.json").read_text(encoding="utf-8"))
    )
    return list(projection.problems)


def _assert_full_closure_excludes_retro_improvement(schema: Any) -> set[str]:
    reachable = _reachable_graphs(schema, "full")
    forbidden = {
        "retro-workflow",
        "improvement-review-workflow",
        "improvement-evaluate-workflow",
        "improvement-export-workflow",
        "improvement-apply-workflow",
        "improvement-rollback-workflow",
    }
    assert reachable.isdisjoint(forbidden)
    assert "inspect-with-issues" in reachable

    uses: set[str] = set()
    for gid in reachable:
        for node in schema.graphs[gid].nodes.values():
            uses.add(node.uses)
    assert "operation:retro-collect" not in uses
    assert "operation:reconcile-improvements" not in uses
    assert "skill:aa-retro" not in uses
    return reachable


# ---------------------------------------------------------------------------
# Scenario 1 — full workflow creates product_bug; never invokes Retro
# ---------------------------------------------------------------------------


def test_scenario_1_full_workflow_product_bug_never_invokes_retro(tmp_path: Path) -> None:
    schema = _load_packaged_schema()
    contracts = load_execution_contracts(Path("."))
    compiled = compile_workflow(schema, contracts)

    _assert_full_closure_excludes_retro_improvement(schema)

    # Runtime path: drive real Issue collect → analyze → reconcile (product code
    # materializes product_bug). Full GraphRuntime/agent is heavier; ops path is
    # the accepted minimum for this acceptance gate.
    project = tmp_path / "proj"
    write_aa_config(project)
    assert not (project / "qa" / "issues" / "problems.json").exists()

    problems = _drive_issue_ops_create_product_bug(project)
    product_bugs = [p for p in problems if p.assessment.classification == "product_bug"]
    assert product_bugs, "expected product_bug Problem from reconcile_issues_operation"
    events_text = (project / "qa" / "issues" / "events.jsonl").read_text(encoding="utf-8")
    assert "problem_detected" in events_text
    assert product_bugs[0].problem_id in events_text

    # Full workflow never invokes Retro / Improvement — no artifacts written.
    assert not (project / "qa" / "retro").exists()
    assert not (project / "qa" / "improvements").exists()
    # Packaged entrypoint exists but is independent of full.
    assert "retro" in compiled.entrypoints
    assert compiled.entrypoints["full"].graph_id == "workflow"


# ---------------------------------------------------------------------------
# Scenario 2 — same misclassification → one prompt_improvement, no new Problem
# ---------------------------------------------------------------------------


def test_scenario_2_shared_misclassification_yields_one_prompt_improvement(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    write_aa_config(project)
    before = _seed_problems(
        project,
        _problem(problem_id="PROB-A", classification="test_bug"),
        _problem(problem_id="PROB-B", classification="test_bug"),
    )

    # Fingerprint omits problem_ids (amendment 4A): same intent merges.
    cand_a = _candidate(
        candidate_id="C-A",
        kind=ImprovementKind.PROMPT,
        delivery=DeliveryKind.MEMORY_PATCH,
        problem_ids=("PROB-A",),
        target=TARGET_MEMORY,
        proposed_change="always seed department name",
    )
    cand_b = _candidate(
        candidate_id="C-B",
        kind=ImprovementKind.PROMPT,
        delivery=DeliveryKind.MEMORY_PATCH,
        problem_ids=("PROB-B",),
        target=TARGET_MEMORY,
        proposed_change="always seed department name",
    )
    assert improvement_fingerprint(cand_a) == improvement_fingerprint(cand_b)

    ctx_a = _retro_context("retro-a", change_ids=("CH-A",), evidence_ids=("PROB-A",))
    _write_retro_run(project, retro_id="retro-a", context=ctx_a, candidates=(cand_a,))
    first = run_retro_accept(project, retro_id="retro-a")
    assert first.result == "accepted"
    assert len(first.improvement_ids) == 1
    improvement_id = first.improvement_ids[0]

    ctx_b = _retro_context("retro-b", change_ids=("CH-B",), evidence_ids=("PROB-B",))
    _write_retro_run(project, retro_id="retro-b", context=ctx_b, candidates=(cand_b,))
    second = run_retro_accept(project, retro_id="retro-b")
    assert second.result == "accepted"
    assert second.improvement_ids == (improvement_id,)

    events = read_improvement_events(project / "qa" / "improvements" / "events.jsonl")
    types = [event.type for event in events]
    assert types.count("improvement_proposed") == 1
    assert "improvement_evidence_linked" in types
    linked = next(event for event in events if event.type == "improvement_evidence_linked")
    assert linked.improvement_id == improvement_id
    assert "PROB-B" in linked.source_refs.problem_ids

    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    assert len(ledger["improvements"]) == 1
    assert ledger["improvements"][improvement_id]["kind"] == "prompt_improvement"
    # Problem Ledger untouched — no new Problem created by Retro.
    assert (project / "qa" / "issues" / "problems.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Scenario 3 — workflow_issue first; separate workflow_improvement later
# ---------------------------------------------------------------------------


def test_scenario_3_workflow_issue_then_separate_workflow_improvement(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    write_aa_config(project)
    issue = _problem(
        problem_id="PROB-TRUNC",
        classification="workflow_issue",
        status="triaged",
    )
    before = _seed_problems(project, issue)

    # Issue exists first with no Improvement ledger.
    assert not (project / "qa" / "improvements").exists()
    loaded = ProblemProjection.model_validate_json(
        (project / "qa" / "issues" / "problems.json").read_text(encoding="utf-8")
    )
    assert loaded.problems[0].assessment.classification == "workflow_issue"
    assert loaded.problems[0].problem_id == "PROB-TRUNC"

    ctx = _retro_context("retro-trunc", evidence_ids=("PROB-TRUNC",))
    cand = _candidate(
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        problem_ids=("PROB-TRUNC",),
        proposed_change=INTENT,
    )
    _write_retro_run(project, retro_id="retro-trunc", context=ctx, candidates=(cand,))
    receipt = run_retro_accept(project, retro_id="retro-trunc")
    assert receipt.result == "accepted"
    improvement_id = receipt.improvement_ids[0]
    assert improvement_id != "PROB-TRUNC"

    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    item = ledger["improvements"][improvement_id]
    assert item["kind"] == "workflow_improvement"
    assert item["state"] == "proposed"
    assert "PROB-TRUNC" in item["source_refs"]["problem_ids"]
    # Problem identity/status unchanged.
    assert (project / "qa" / "issues" / "problems.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Scenario 4 — same window, new Retro ID → evidence_linked, one Improvement
# ---------------------------------------------------------------------------


def test_scenario_4_same_intent_across_retro_ids_links_evidence(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    write_aa_config(project)
    _seed_problems(project, _problem(problem_id="PROB-1"), _problem(problem_id="PROB-2"))

    shared_intent = INTENT
    cand1 = _candidate(
        candidate_id="C-1",
        problem_ids=("PROB-1",),
        proposed_change=shared_intent,
    )
    cand2 = _candidate(
        candidate_id="C-2",
        problem_ids=("PROB-2",),
        proposed_change=shared_intent,
    )
    expected_id = improvement_id_for_fingerprint(improvement_fingerprint(cand1))

    ctx1 = _retro_context("retro-window-1", evidence_ids=("PROB-1",))
    _write_retro_run(project, retro_id="retro-window-1", context=ctx1, candidates=(cand1,))
    first = run_retro_accept(project, retro_id="retro-window-1")
    assert first.improvement_ids == (expected_id,)
    first_events = (project / "qa" / "improvements" / "events.jsonl").read_bytes()

    ctx2 = _retro_context("retro-window-2", evidence_ids=("PROB-2",))
    _write_retro_run(project, retro_id="retro-window-2", context=ctx2, candidates=(cand2,))
    second = run_retro_accept(project, retro_id="retro-window-2")
    assert second.improvement_ids == (expected_id,)

    events = read_improvement_events(project / "qa" / "improvements" / "events.jsonl")
    assert sum(1 for event in events if event.type == "improvement_proposed") == 1
    linked = [event for event in events if event.type == "improvement_evidence_linked"]
    assert len(linked) == 1
    assert linked[0].retro_id == "retro-window-2"
    assert linked[0].improvement_id == expected_id
    assert "PROB-2" in linked[0].source_refs.problem_ids
    # First run's propose event bytes remain a prefix (append-only).
    assert (project / "qa" / "improvements" / "events.jsonl").read_bytes().startswith(first_events)

    status2 = json.loads(
        (project / "qa" / "retro" / "retro-window-2" / "accept-status.json").read_text(encoding="utf-8")
    )
    assert status2["result"] == "accepted"
    assert status2["improvement_ids"] == [expected_id]
    assert status2["event_ids"]


# ---------------------------------------------------------------------------
# Scenario 5 — apply Improvement; Problem unchanged until Issue verification
# ---------------------------------------------------------------------------


def test_scenario_5_apply_improvement_leaves_problem_unchanged(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    write_aa_config(project)
    (project / ".aa" / "memory").mkdir(parents=True)
    (project / ".aa" / "memory" / "aa-run.md").write_text("# memory\n", encoding="utf-8")
    problem = _problem(
        problem_id="PROB-1",
        classification="test_bug",
        status="triaged",
        authority="human_confirmed",
    )
    before = _seed_problems(project, problem)

    store = ProjectImprovementStore(project)
    imp_id = "IMP-APPLY0000000000001"
    store.append_and_rebuild(
        [
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": "IMPEVT-PROP",
                    "idempotency_key": "IDEM-PROP",
                    "ts": "2026-07-26T00:00:00Z",
                    "improvement_id": imp_id,
                    "expected_improvement_version": 0,
                    "type": "improvement_proposed",
                    "fingerprint": "a" * 64,
                    "fingerprint_version": "1",
                    "kind": "prompt_improvement",
                    "delivery": "memory_patch",
                    "source_refs": {"problem_ids": ["PROB-1"]},
                    "target": TARGET_MEMORY,
                    "rationale": "Fixture seeding",
                    "proposed_change": "always seed department name",
                    "verification": {
                        "suites": ["workflow-run"],
                        "success_criteria": "eval passes",
                    },
                    "risk": "low",
                    "confidence": "high",
                    "retro_id": "retro-1",
                    "candidate_id": "C-1",
                    "context_sha256": "c" * 64,
                    "candidate_batch_digest": "d" * 64,
                }
            ),
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 2,
                    "event_id": "IMPEVT-APP",
                    "idempotency_key": "IDEM-APP",
                    "ts": "2026-07-26T00:01:00Z",
                    "improvement_id": imp_id,
                    "expected_improvement_version": 1,
                    "type": "improvement_review_approved",
                    "who": "reviewer",
                    "reason": "ok",
                    "review_id": "REV-1",
                }
            ),
        ]
    )
    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    improvement = ImprovementProjection.model_validate(ledger["improvements"][imp_id])

    delivery = MemoryPatchDelivery(project, eval_runner=_passing_runner())
    delivery.evaluate(improvement)
    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][imp_id])
    before_sha = sha256_bytes((project / ".aa" / "memory" / "aa-run.md").read_bytes())
    delivery.apply(current, expected_target_sha256=before_sha)

    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][imp_id]["state"] == ImprovementState.APPLIED.value
    # Authoritative Problem status stays triaged — Issue verification is separate.
    assert (project / "qa" / "issues" / "problems.json").read_bytes() == before
    events_path = project / "qa" / "issues" / "events.jsonl"
    assert "problem_resolved" not in events_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Scenario 6 — knowledge export eligibility; L1 identical before promotion
# ---------------------------------------------------------------------------


def test_scenario_6_knowledge_export_requires_verified_problem_and_preserves_l1(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    write_aa_config(project)
    l1_before = _seed_l1(project)

    unresolved = _problem(
        problem_id="PROB-K",
        classification="test_data_issue",
        status="triaged",
        authority="human_confirmed",
    )
    resolved = _problem(
        problem_id="PROB-K",
        classification="test_data_issue",
        status="resolved",
        authority="human_confirmed",
    )
    # Bypass model min_length so we can pin the eligibility gate on empty scope.
    assert resolved.resolution is not None
    empty_scope = Problem.model_construct(
        problem_id=resolved.problem_id,
        fingerprint=resolved.fingerprint,
        title=resolved.title,
        assessment=resolved.assessment,
        status=resolved.status,
        first_seen=resolved.first_seen,
        last_seen=resolved.last_seen,
        occurrences=list(resolved.occurrences),
        version=resolved.version,
        resolution=ProblemResolution.model_construct(
            resolved_at=resolved.resolution.resolved_at,
            change_id=resolved.resolution.change_id,
            batch_id=resolved.resolution.batch_id,
            disposition=resolved.resolution.disposition,
            verification_scope=[],
            evidence_digest=resolved.resolution.evidence_digest,
        ),
    )

    store = ProjectImprovementStore(project)
    imp_id = "IMP-KNOW00000000000001"
    delta = DataKnowledgeProposal.model_validate(
        {
            "schema_version": "1",
            "mode": "delta",
            "entities": {"dept": {"required_fields": ["name"]}},
        }
    )
    store.append_and_rebuild(
        [
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": "IMPEVT-PROP",
                    "idempotency_key": "IDEM-PROP",
                    "ts": "2026-07-26T00:00:00Z",
                    "improvement_id": imp_id,
                    "expected_improvement_version": 0,
                    "type": "improvement_proposed",
                    "fingerprint": "k" * 64,
                    "fingerprint_version": "1",
                    "kind": "domain_knowledge",
                    "delivery": "knowledge_delta",
                    "source_refs": {"problem_ids": ["PROB-K"]},
                    "target": ".aa/data-knowledge.yaml",
                    "rationale": "Stable dept rule",
                    "proposed_change": "Require dept.name",
                    "knowledge_delta": delta.model_dump(mode="json"),
                    "verification": {
                        "suites": ["workflow-run"],
                        "success_criteria": "L2 validates",
                    },
                    "risk": "low",
                    "confidence": "high",
                    "retro_id": "retro-k",
                    "candidate_id": "C-K",
                    "context_sha256": "c" * 64,
                    "candidate_batch_digest": "d" * 64,
                }
            ),
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 2,
                    "event_id": "IMPEVT-APP",
                    "idempotency_key": "IDEM-APP",
                    "ts": "2026-07-26T00:01:00Z",
                    "improvement_id": imp_id,
                    "expected_improvement_version": 1,
                    "type": "improvement_review_approved",
                    "who": "reviewer",
                    "reason": "ok",
                    "review_id": "REV-1",
                }
            ),
        ]
    )
    ledger = json.loads((project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8"))
    improvement = ImprovementProjection.model_validate(ledger["improvements"][imp_id])
    delivery = KnowledgeDeltaDelivery(project)

    with pytest.raises(ImprovementDeliveryError, match="eligibility|status|resolved"):
        delivery.export(improvement, problems={"PROB-K": unresolved})
    assert (project / ".aa" / "data-knowledge.yaml").read_bytes() == l1_before
    assert not (project / "qa" / "improvements" / "knowledge-delta").exists()

    with pytest.raises(ImprovementDeliveryError, match="verified resolution scope"):
        delivery.export(improvement, problems={"PROB-K": empty_scope})
    assert (project / ".aa" / "data-knowledge.yaml").read_bytes() == l1_before
    assert not (project / "qa" / "improvements" / "knowledge-delta").exists()

    receipt = delivery.export(improvement, problems={"PROB-K": resolved})
    assert receipt.created is True
    assert (project / ".aa" / "data-knowledge.yaml").read_bytes() == l1_before
    proposal = project / "qa" / "improvements" / "knowledge-delta" / f"{imp_id}.proposal.yaml"
    assert proposal.is_file()
    raw = yaml.safe_load(proposal.read_text(encoding="utf-8"))
    assert raw["mode"] == "delta"
    assert "dept" in raw["entities"]

    events = read_improvement_events(project / "qa" / "improvements" / "events.jsonl")
    assert any(event.type == "improvement_exported" for event in events)
    # No L1 promote happened — knowledge promote is a separate CLI path.
    assert (project / ".aa" / "data-knowledge.yaml").read_bytes() == l1_before


# ---------------------------------------------------------------------------
# Step 4 — packaged graph-order regression
# ---------------------------------------------------------------------------


def test_packaged_graph_inspect_before_decide_and_report() -> None:
    schema = _load_packaged_schema()

    assurance_edges = _edge_pairs(schema, "assurance")
    assert ("execution", "inspect-with-issues") in assurance_edges
    assert ("inspect-with-issues", "healing") in assurance_edges
    assert ("healing", "report") in assurance_edges

    healing_edges = _edge_pairs(schema, "healing")
    assert ("rerun", "inspect-with-issues") in healing_edges
    assert ("inspect-with-issues", "decide") in healing_edges

    full_reachable = _reachable_graphs(schema, "full")
    assert "inspect-with-issues" in full_reachable
    assert "retro-workflow" not in full_reachable
    for name in (
        "improvement-review-workflow",
        "improvement-evaluate-workflow",
        "improvement-export-workflow",
        "improvement-apply-workflow",
        "improvement-rollback-workflow",
    ):
        assert name not in full_reachable

    # Retro / Improvement entrypoints exist but are independent.
    assert _entrypoint_graph(schema, "retro") == "retro-workflow"
    assert "improvement-review" in schema.entrypoints
