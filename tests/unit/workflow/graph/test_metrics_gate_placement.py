"""Where the metrics-sufficiency gate rules, driven rather than read.

Mirrors ``test_trace_gate_placement.py``: plan one superstep at a time through the
real planner, evaluate the metrics gate against a real ``inspect/metrics.json`` via
``evaluate_metrics_sufficiency``, and observe consequences:

- a stop-worthy metrics document does not pre-empt healing;
- the same document stops before ``report`` once healing (+ materialize) returned;
- ``needs_human_review`` interrupts only after healing; resume chooses report path
  (via the subsequent trace gate) or stop;
- post-fixer healing aborts never reach the metrics gate (STOP, not complete-failed).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricsDocument
from assurance_agent.artifacts.policy import load_policy_bytes
from assurance_agent.workflow.graph.compiler import compile_workflow, resolve_params
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    InterruptProjection,
    ResolvedArtifact,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import plan_superstep
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view

CHANGE_ID = "CH-1"
GATE_NODE = "metrics-sufficiency"
INTERRUPT_NODE = "metrics-sufficiency-review"
TRACE_GATE_NODE = "trace-sufficiency"
TRACE_INTERRUPT_NODE = "trace-sufficiency-review"
MATERIALIZE = "materialize-pr-metrics"
COLLECT = "collect-pr-metrics-batch"
GATE_ID = "metrics-sufficiency-gate"
TRACE_GATE_ID = "trace-sufficiency-gate"
METRICS_REL = "inspect/metrics.json"
TRACE_REL = "inspect/trace-sufficiency.json"


def _metrics(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "2",
        "change_id": CHANGE_ID,
        "cadence": "pr",
        "computed_at": "2026-08-05T02:00:00+00:00",
        "risk_tier": "low",
        "risk_tier_lower_bound": "low",
        "risk_tier_declared": None,
        "risk_declaration_lowered": False,
        "risk_lowered_declarations": [],
        "metrics": {
            "constraint_coverage": {
                "layer": "api",
                "status": "evaluated",
                "value": 0.9,
                "declared": {"total": 10, "covered": 9, "value": 0.9, "uncovered": []},
                "touched": None,
                "holds": None,
                "surfaces": [],
                "evidence": "constraint-coverage.json",
            }
        },
        "collection_gaps": [],
        "shortboards": [],
        "floor_ratio": None,
        "policy_digest": "0" * 64,
    }
    document.update(overrides)
    return document


HEALTHY = _metrics()
GAPPED = _metrics(
    metrics={
        "constraint_coverage": {
            "layer": "api",
            "status": "collection_failed",
            "value": None,
            "declared": None,
            "touched": None,
            "holds": None,
            "surfaces": [],
            "evidence": "",
        }
    },
    collection_gaps=[
        {
            "code": "collection_failed",
            "detail": "constraint cover missing",
            "metric": "constraint_coverage",
            "subject": "",
        }
    ],
)
# Measured (not gapped) boolean floor miss on a non-critical tier: the hard
# rule at `evaluate_metrics_sufficiency` returns `needs_human`, independent of
# `collection_gaps`/`reject` — exercises the interrupt machinery that GAPPED no
# longer reaches now that a collection gap routes straight to `reject`.
MEASURED_MISS = _metrics(
    metrics={
        "adversarial_clean": {
            "layer": "cross",
            "status": "evaluated",
            "value": None,
            "declared": None,
            "touched": None,
            "holds": False,
            "surfaces": [],
            "evidence": "adversarial-yield.json",
        }
    },
)


def _trace_facts() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": "20260805-020000",
        "policy_digest": "0" * 64,
        "as_of": "2026-08-05T02:00:00+00:00",
        "integrity": "complete",
        "integrity_blocks_routing": False,
        "sufficient": True,
        "has_open_problems": False,
        "error_code": None,
        "insufficient_cases": [],
        "gap_codes": [],
    }


def _policy_text(on_insufficient: str = "require_human") -> str:
    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["on_insufficient"] = on_insufficient
    return yaml.safe_dump(policy, sort_keys=False)


def _project(
    tmp_path: Path, metrics: dict[str, object] | None, *, on_insufficient: str = "require_human"
) -> Path:
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aa" / "policy.yaml").write_text(_policy_text(on_insufficient), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    (change_dir / TRACE_REL).write_text(json.dumps(_trace_facts()), encoding="utf-8")
    if metrics is not None:
        (change_dir / METRICS_REL).write_text(json.dumps(metrics), encoding="utf-8")
    return change_dir


class _FakeArtifacts:
    def __init__(self, payloads: dict[tuple[str, str], object] | None = None) -> None:
        self._payloads = payloads or {}

    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        payload = self._payloads[(tree_id, logical_path)]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return ResolvedArtifact(
            value=payload,
            reads_sha256={logical_path: hashlib.sha256(canonical.encode("utf-8")).hexdigest()},
        )


def _compiled() -> CompiledWorkflow:
    return compile_workflow(load_workflow_v2(Path.cwd()), load_execution_contracts(Path.cwd()))


def _context(tmp_path: Path, change_dir: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id=CHANGE_ID,
    )


def _projection(
    compiled: CompiledWorkflow,
    graph_id: str,
    params: dict[str, object],
    *,
    tasks: list[TaskProjection],
    interrupts: dict[str, InterruptProjection],
    supersteps: int,
) -> GraphProjection:
    return GraphProjection(
        invocation_id=f"inv-{graph_id}",
        entrypoint=graph_id,
        checkpoint_ns=f"inv-{graph_id}",
        parent_invocation_id="inv-parent",
        parent_task_id="parent-task",
        structural_path=f"execute-workflow/{graph_id}/{graph_id}",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=params,
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=supersteps,
        tasks={task.task_id: task for task in tasks},
        interrupts=interrupts,
    )


def _succeeded(task: ExecutableTask, **overrides: object) -> TaskProjection:
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "node_id": task.node_id,
        "status": "succeeded",
        # D14: planner only advances past a predecessor once outputs are committed.
        "outputs_committed": True,
        "attempts_used": 1,
        "latest_attempt_id": f"{task.task_id}-a1",
    }
    payload.update(overrides)
    return TaskProjection(**payload)  # type: ignore[arg-type]


class _Run:
    def __init__(self, order: list[str], terminal: str | None, reason: str | None) -> None:
        self.order = order
        self.terminal = terminal
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return f"_Run(order={self.order}, terminal={self.terminal!r}, reason={self.reason!r})"


_TASK_STATUS_FOR_TERMINAL = {"end": "succeeded", "stop": "stopped", "fail": "failed"}


def _gate_report(
    tmp_path: Path,
    change_dir: Path,
    node: object,
    verdicts: dict[str, str] | None,
    remaining_eligible: list[bool],
) -> dict[str, object]:
    with_ = getattr(node, "with_", None) or {}
    gate_id = with_.get("gate")
    if gate_id in {GATE_ID, TRACE_GATE_ID}:
        report = check_gate_in_view(
            _compiled().schema.gates,
            gate_id,
            GateEvaluationContext(
                project_root=tmp_path,
                repo_root=tmp_path,
                change_dir=change_dir,
                change_id=CHANGE_ID,
                params={},
                state_values={},
                node_results={},
            ),
        )
        return {"gate_id": gate_id, "verdict": report.verdict.value, "value": report.verdict.value}
    if gate_id is not None:
        verdict = (verdicts or {})[gate_id]
        return {"gate_id": gate_id, "verdict": verdict, "value": verdict}
    value = remaining_eligible.pop(0) if remaining_eligible else True
    return {"expression": with_.get("expression", ""), "value": value}


def _drive(
    tmp_path: Path,
    change_dir: Path,
    *,
    graph_id: str,
    verdicts: dict[str, str] | None = None,
    resume_action: str | None = None,
    artifacts: _FakeArtifacts | None = None,
    values: dict[str, object] | None = None,
    subgraph_terminals: dict[str, str] | None = None,
    eligible: list[bool] | None = None,
    until: Callable[[list[str]], bool] | None = None,
    limit: int = 40,
) -> _Run:
    compiled = _compiled()
    graph = compiled.schema.graphs[graph_id]
    params = resolve_params(compiled.schema, {"run_mode": "full", "test_types": ["api", "e2e"]})
    context = _context(tmp_path, change_dir)
    tasks: list[TaskProjection] = []
    interrupts: dict[str, InterruptProjection] = {}
    order: list[str] = []
    remaining_eligible = list(eligible or ())

    for step in range(limit):
        if until is not None and until(order):
            return _Run(order, None, None)
        plan = plan_superstep(
            compiled,
            _projection(
                compiled,
                graph_id,
                params,
                tasks=tasks,
                interrupts=interrupts,
                supersteps=step,
            ),
            context,
            artifacts or _FakeArtifacts(),
        )
        if plan.terminal is not None:
            return _Run(order, plan.terminal, plan.reason)
        if not plan.tasks:
            return _Run(order, None, plan.reason)
        for task in plan.tasks:
            order.append(task.node_id)
            node = graph.nodes[task.node_id]
            if node.uses == "builtin:gate":
                tasks.append(
                    _succeeded(
                        task,
                        gate_report=_gate_report(tmp_path, change_dir, node, verdicts, remaining_eligible),
                    )
                )
            elif node.uses == "builtin:interrupt":
                interrupts[task.task_id] = InterruptProjection(
                    interrupt_id=task.task_id,
                    checkpoint_ns=f"inv-{graph_id}",
                    node_id=task.node_id,
                    checkpoint=node.interrupt.checkpoint if node.interrupt is not None else "",
                    actions=tuple(node.interrupt.actions if node.interrupt is not None else ()),
                    audited_reads_sha256={},
                    resolved_action=resume_action,
                )
                tasks.append(_succeeded(task))
            elif task.node_id in (subgraph_terminals or {}):
                terminal = (subgraph_terminals or {})[task.node_id]
                tasks.append(_succeeded(task, status=_TASK_STATUS_FOR_TERMINAL[terminal]))
            else:
                tasks.append(_succeeded(task, value=(values or {}).get(task.node_id)))
    raise AssertionError(f"graph {graph_id} did not settle within {limit} supersteps: {order}")


_HEALING_VERDICTS = {
    "healing-entry-gate": "enter",
    "fixer-safety-gate": "pass",
    "fixer-proposal-approval-gate": "pass",
}
_PROPOSAL: dict[tuple[str, str], object] = {
    ("tree-0", "change:healing/fix-proposal.json"): {"proposals": [{"target": "api", "eligible": True}]}
}
_HEALING_VALUES: dict[str, object] = {"fixer-authority-ready": {"route": "pass"}}


def _drive_healing(
    tmp_path: Path,
    change_dir: Path,
    *,
    decide: str = "exit",
    safety: str = "pass",
    resume_action: str | None = None,
    eligible: list[bool] | None = None,
    until: Callable[[list[str]], bool] | None = None,
) -> _Run:
    return _drive(
        tmp_path,
        change_dir,
        graph_id="healing",
        verdicts={
            **_HEALING_VERDICTS,
            "fixer-safety-gate": safety,
            "healing-loop-gate": decide,
        },
        resume_action=resume_action,
        artifacts=_FakeArtifacts(_PROPOSAL),
        values=_HEALING_VALUES,
        eligible=eligible,
        until=until,
    )


def _drive_assurance(
    tmp_path: Path,
    change_dir: Path,
    *,
    resume_action: str | None = None,
    subgraph_terminals: dict[str, str] | None = None,
) -> _Run:
    return _drive(
        tmp_path,
        change_dir,
        graph_id="assurance",
        verdicts=_HEALING_VERDICTS,
        resume_action=resume_action,
        subgraph_terminals=subgraph_terminals,
    )


def test_stop_worthy_metrics_do_not_prevent_healing(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, GAPPED)
    run = _drive_healing(tmp_path, change_dir, decide="exit")
    assert "fix-api" in run.order
    assert COLLECT not in run.order
    assert "rerun" in run.order
    assert GATE_NODE not in run.order
    assert run.order[-1] == "complete-resolved"


def test_healing_precedes_materialize_and_metrics_gate(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, HEALTHY)
    run = _drive_assurance(tmp_path, change_dir)
    assert run.order.index("healing") < run.order.index(MATERIALIZE)
    assert run.order.index(MATERIALIZE) < run.order.index(GATE_NODE)
    assert run.order.index(GATE_NODE) < run.order.index(TRACE_GATE_NODE)


def test_healthy_metrics_reach_the_report(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, HEALTHY)
    run = _drive_assurance(tmp_path, change_dir)
    assert run.order[-1] == "report"
    assert run.terminal == "end"


def test_collection_gaps_reject_reaches_report_without_interrupt(tmp_path: Path) -> None:
    """``reject`` blocks archive (see ``archive-gate``) but never pauses for human input."""
    change_dir = _project(tmp_path, GAPPED, on_insufficient="require_human")
    run = _drive_assurance(tmp_path, change_dir)
    assert GATE_NODE in run.order
    assert INTERRUPT_NODE not in run.order
    assert TRACE_GATE_NODE in run.order
    assert run.order[-1] == "report"
    assert run.terminal == "end"


def test_measured_miss_stop_resume_at_metrics_interrupt_stops_before_report(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, MEASURED_MISS)
    run = _drive_assurance(tmp_path, change_dir, resume_action="stop")
    assert GATE_NODE in run.order
    assert "report" not in run.order
    assert run.terminal == "stop"


def test_accept_risk_at_metrics_interrupt_continues_toward_report(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, MEASURED_MISS)
    run = _drive_assurance(tmp_path, change_dir, resume_action="accept_risk")
    assert run.order.index("healing") < run.order.index(INTERRUPT_NODE)
    assert TRACE_GATE_NODE in run.order or TRACE_INTERRUPT_NODE in run.order or run.order[-1] == "report"
    assert run.terminal == "end"
    assert run.order[-1] == "report"


@pytest.mark.parametrize("safety", ["stop", "reject"], ids=["refused", "rejected"])
def test_post_fixer_abort_never_reaches_metrics_gate_or_report(tmp_path: Path, safety: str) -> None:
    change_dir = _project(tmp_path, HEALTHY)
    child = _drive_healing(tmp_path, change_dir, safety=safety)
    assert child.terminal == "stop"
    parent = _drive_assurance(tmp_path, change_dir, subgraph_terminals={"healing": child.terminal})
    assert parent.order[-1] == "healing"
    assert GATE_NODE not in parent.order
    assert MATERIALIZE not in parent.order
    assert "report" not in parent.order
    assert parent.terminal == "stop"


def test_metrics_document_model_roundtrip_matches_gate_input() -> None:
    """Harness sanity: the JSON shape the drive writes is a valid MetricsDocument."""
    doc = MetricsDocument.model_validate(HEALTHY)
    assert doc.cadence == "pr"
    gapped = MetricsDocument.model_validate(GAPPED)
    assert gapped.collection_gaps
    assert isinstance(gapped.collection_gaps[0], MetricCollectionGap)
