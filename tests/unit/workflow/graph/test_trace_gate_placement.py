"""Where the trace-sufficiency gate rules, driven rather than read.

The structural claims in ``test_trace_sufficiency_gate.py`` say the gate sits in the
``assurance`` graph after ``healing``. That is the *shape*; what matters is the
consequence, and a consequence is easier to get wrong than a shape. So these tests
drive the packaged graphs one superstep at a time through the real planner, executing
each gate against a real facts document on disk with the real gate evaluator, and
observe what happens:

- a facts document that says ``stop`` does not stop **healing** — the healing loop
  still reaches its own ``decide`` gate and can still iterate, because nothing inside
  healing reads that document;
- the same document *does* stop the run before ``report`` once healing has returned;
- ``needs_human_review`` interrupts only after healing returns, and the resume action
  decides between the report and a stop;
- healing's **post-fixer aborts never return to the gate at all**. Once ``fix-api`` /
  ``fix-e2e`` have written tests that no ``rerun`` has executed, the facts document on
  disk describes the previous batch, so a ``pass`` there would vouch for evidence that
  no longer matches the tests. Those paths leave the subgraph as a ``STOP``, which the
  parent turns into its own ``STOP`` — driven here at both levels, with a *healthy*
  facts document, so the test fails if the gate is ever reached.

The harness is deliberately thin: `plan_superstep` decides *what runs next*, and the
"executor" here only marks tasks succeeded, computing gate verdicts with
``check_gate_in_view`` so the verdicts under test are the ones the runtime would get.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

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
GATE_NODE = "trace-sufficiency"
INTERRUPT_NODE = "trace-sufficiency-review"
GATE_ID = "trace-sufficiency-gate"
METRICS_GATE_ID = "metrics-sufficiency-gate"
FACTS_REL = "inspect/trace-sufficiency.json"


# --------------------------------------------------------------------------- #
# fixtures: a project whose only interesting artifact is the facts document
# --------------------------------------------------------------------------- #


def _facts(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": "20260702-111111",
        "policy_digest": "0" * 64,
        "as_of": "2026-07-02T11:11:11+00:00",
        "integrity": "complete",
        "integrity_blocks_routing": False,
        "sufficient": True,
        "has_open_problems": False,
        "error_code": None,
        "insufficient_cases": [],
        "gap_codes": [],
    }
    document.update(overrides)
    return document


HEALTHY = _facts()
OPEN_BUG = _facts(has_open_problems=True)
THIN = _facts(
    sufficient=False,
    insufficient_cases=[{"case_id": "TC_API_001", "reason_codes": ["never_run"]}],
)


def _policy_text(on_insufficient: str) -> str:
    from assurance_agent.artifacts.policy import load_policy_bytes

    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["on_insufficient"] = on_insufficient
    return yaml.safe_dump(policy, sort_keys=False)


def _healthy_metrics() -> dict[str, object]:
    """Passing PR metrics so the metrics gate does not pre-empt the trace gate."""
    return {
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


def _project(tmp_path: Path, facts: dict[str, object] | None, *, on_insufficient: str = "block") -> Path:
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aa" / "policy.yaml").write_text(_policy_text(on_insufficient), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    if facts is not None:
        (change_dir / FACTS_REL).write_text(json.dumps(facts), encoding="utf-8")
    # Task 8: metrics gate sits before the trace gate; give it a healthy document
    # so these tests keep isolating the *trace* verdict.
    (change_dir / "inspect" / "metrics.json").write_text(json.dumps(_healthy_metrics()), encoding="utf-8")
    return change_dir


# --------------------------------------------------------------------------- #
# harness: plan / execute / repeat over a packaged graph
# --------------------------------------------------------------------------- #


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
        "attempts_used": 1,
        "latest_attempt_id": f"{task.task_id}-a1",
    }
    payload.update(overrides)
    return TaskProjection(**payload)  # type: ignore[arg-type]


class _Run:
    """What a drive produced: the activation order and how the graph ended."""

    def __init__(self, order: list[str], terminal: str | None, reason: str | None) -> None:
        self.order = order
        self.terminal = terminal
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"_Run(order={self.order}, terminal={self.terminal!r}, reason={self.reason!r})"


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
    limit: int = 30,
) -> _Run:
    """Plan, execute, repeat — the runtime's loop with a trivial executor.

    Named gates are evaluated for real against the project on disk, so the routes the
    planner takes are the ones the runtime would take. ``verdicts`` is only for gates
    this test does not want to model (``healing-entry-gate`` and friends), and an
    interrupt resolves to ``resume_action``.

    ``eligible`` supplies successive values for healing's inline ``proposal-eligible``
    expression, one per activation, defaulting to ``True``. A loop that re-enters
    ``proposal`` evaluates it more than once, and the values differing across attempts
    is the whole shape of the defect this file's last section covers.

    ``subgraph_terminals`` maps a ``graph:`` node onto how its child graph ended.
    ``_child_result_to_task_result`` turns a stopped child into a stopped *task*, and
    the planner turns a stopped task into the parent's own terminal, so replaying a
    child's terminal through ``_TASK_STATUS_FOR_TERMINAL`` is how a parent-level
    consequence of a child-level route gets observed here.

    ``until`` stops the drive early. Needed for the healing loop, whose ``continue``
    verdict is bounded at runtime by the attempt budget — which is staged by the
    scheduler, not the planner, so a harness this thin would iterate forever.
    """
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


# How ``GraphRuntime._child_result_to_task_result`` reports a finished child graph
# to the parent task that hosts it.
_TASK_STATUS_FOR_TERMINAL = {"end": "succeeded", "stop": "stopped", "fail": "failed"}


def _gate_report(
    tmp_path: Path,
    change_dir: Path,
    node: object,
    verdicts: dict[str, str] | None,
    remaining_eligible: list[bool],
) -> dict[str, object]:
    """Evaluate a named gate for real; fall back to the supplied verdict otherwise."""
    with_ = getattr(node, "with_", None) or {}
    gate_id = with_.get("gate")
    if gate_id in {GATE_ID, METRICS_GATE_ID}:
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
    # An inline-expression gate (healing's `proposal-eligible`): value only.
    value = remaining_eligible.pop(0) if remaining_eligible else True
    return {"expression": with_.get("expression", ""), "value": value}


# --------------------------------------------------------------------------- #
# healing is never pre-empted by a trace verdict
# --------------------------------------------------------------------------- #

_HEALING_VERDICTS = {"healing-entry-gate": "enter", "fixer-safety-gate": "pass"}
_PROPOSAL: dict[tuple[str, str], object] = {
    ("tree-0", "change:healing/fix-proposal.json"): {"proposals": [{"target": "api", "eligible": True}]}
}


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
        eligible=eligible,
        until=until,
    )


@pytest.mark.parametrize("facts", [OPEN_BUG, THIN], ids=["open_product_bug", "thin_evidence"])
def test_an_adjudication_worthy_facts_document_does_not_prevent_healing(
    tmp_path: Path, facts: dict[str, object]
) -> None:
    """The Critical property, driven.

    With a facts document on disk that the gate will later adjudicate, healing still runs
    the fixers, still reruns the tests, still re-inspects, and still reaches its own
    ``decide`` gate. That is only true because nothing in the healing loop reads that
    document — the adjudication happens above, after healing returns.
    """
    change_dir = _project(tmp_path, facts)

    run = _drive_healing(tmp_path, change_dir, decide="exit")

    assert "fix-api" in run.order
    assert "rerun" in run.order
    assert "inspect-with-issues" in run.order
    assert "decide" in run.order
    assert GATE_NODE not in run.order and INTERRUPT_NODE not in run.order
    assert run.order[-1] == "complete-resolved"


def test_the_healing_loop_can_still_iterate_before_trace_adjudication(tmp_path: Path) -> None:
    """``continue`` must send control back to ``proposal`` for another attempt.

    A trace verdict reached inside the rerun's inspection would have ended the run
    here instead, and the attempt budget — the thing that is supposed to decide when
    to give up — would never have been consulted.
    """
    change_dir = _project(tmp_path, OPEN_BUG)

    run = _drive_healing(
        tmp_path,
        change_dir,
        decide="continue",
        until=lambda order: order.count("proposal") >= 2,
    )

    assert run.order.count("proposal") == 2, run
    assert run.order.count("decide") == 1, run
    assert GATE_NODE not in run.order


def test_the_inspection_the_healing_loop_waits_on_always_completes(tmp_path: Path) -> None:
    """The rerun's inspect subgraph reaches its own END regardless of the facts.

    ``decide`` is only planned once ``inspect-with-issues`` succeeds, so an inspection
    that could stop on its own verdict would strand the healing loop.
    """
    change_dir = _project(tmp_path, OPEN_BUG)

    run = _drive(
        tmp_path,
        change_dir,
        graph_id="inspect-with-issues",
        # A batch with no abnormal results: the analyzer is skipped and the empty
        # analysis is recorded, which is the shortest path through the graph.
        values={"collect-observations": {"abnormal_count": 0}},
    )

    assert run.terminal == "end"
    assert run.order[-1] == "inspect-complete"
    assert "materialize-trace-projection" in run.order


# --------------------------------------------------------------------------- #
# adjudication happens after healing, before the report
# --------------------------------------------------------------------------- #


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


def test_healing_precedes_the_gate_in_the_order_actually_planned(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_assurance(tmp_path, change_dir)

    assert run.order.index("healing") < run.order.index(GATE_NODE)
    assert run.order.index("inspect-with-issues") < run.order.index("healing")
    # Task 8: metrics materialize + gate sit between healing and the trace gate.
    assert run.order.index("healing") < run.order.index("materialize-pr-metrics")
    assert run.order.index("materialize-pr-metrics") < run.order.index("metrics-sufficiency")
    assert run.order.index("metrics-sufficiency") < run.order.index(GATE_NODE)


def test_sufficient_evidence_reaches_the_report(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_assurance(tmp_path, change_dir)

    assert run.order[-1] == "report"
    assert run.terminal == "end"


def test_blocked_thin_evidence_ends_the_run_before_the_report(tmp_path: Path) -> None:
    """Untrustworthy evidence still pre-empts the report."""
    change_dir = _project(tmp_path, THIN, on_insufficient="block")

    run = _drive_assurance(tmp_path, change_dir)

    assert run.order[-1] == GATE_NODE
    assert "report" not in run.order
    assert run.terminal == "stop"


def test_an_open_product_problem_reaches_a_failure_report(tmp_path: Path) -> None:
    """A trusted product failure blocks release without truncating analysis."""
    change_dir = _project(tmp_path, OPEN_BUG, on_insufficient="block")

    run = _drive_assurance(tmp_path, change_dir)

    assert run.order[-2:] == [GATE_NODE, "report"]
    assert run.terminal == "end"


def test_a_missing_facts_document_stops_before_the_report(tmp_path: Path) -> None:
    """Inspection always materializes, so an absent document means something broke —
    and a gate that cannot read its input must not be mistaken for one that passed."""
    change_dir = _project(tmp_path, None)

    run = _drive_assurance(tmp_path, change_dir)

    assert "report" not in run.order
    assert run.terminal == "stop"


def test_thin_evidence_interrupts_only_after_healing_returned(tmp_path: Path) -> None:
    """``needs_human_review`` reaches a person, and not before healing is finished.

    An interrupt raised inside the healing rerun's inspection would have suspended
    the loop mid-attempt and asked a human about evidence the fixers were still
    working on.
    """
    change_dir = _project(tmp_path, THIN, on_insufficient="require_human")

    run = _drive_assurance(tmp_path, change_dir, resume_action="accept_risk")

    assert run.order.index("healing") < run.order.index(INTERRUPT_NODE)
    assert run.order[-1] == "report"


def test_a_human_stop_at_the_interrupt_keeps_the_report_unwritten(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, THIN, on_insufficient="require_human")

    run = _drive_assurance(tmp_path, change_dir, resume_action="stop")

    assert INTERRUPT_NODE in run.order
    assert "report" not in run.order
    assert run.terminal == "stop"


def test_warn_policy_lets_thin_evidence_through_to_the_report(tmp_path: Path) -> None:
    """The same document, a different policy, no code change: the DSL read is real."""
    change_dir = _project(tmp_path, THIN, on_insufficient="warn")

    run = _drive_assurance(tmp_path, change_dir)

    assert INTERRUPT_NODE not in run.order
    assert run.order[-1] == "report"
    assert run.terminal == "end"


# --------------------------------------------------------------------------- #
# a post-fixer abort is never adjudicated
# --------------------------------------------------------------------------- #
#
# Every drive below installs HEALTHY — a facts document the gate passes. It is the
# only choice that can fail: the run must stop because of *where* it aborted, not
# because the document happened to say something bad. A document that already said
# `stop` would make these tests pass under the pre-fix routing too.


@pytest.mark.parametrize("safety", ["stop", "reject"], ids=["refused", "rejected"])
def test_a_safety_refusal_after_the_fixers_stops_healing_itself(tmp_path: Path, safety: str) -> None:
    """``fixer-safety-gate`` refusing leaves the subgraph as a STOP, not a completion.

    The fixers have already written to ``tests/`` at this point and ``rerun`` has not
    run, so no completion status healing could record would be true of the evidence on
    disk. ``complete-failed`` — which *is* a completion, and would let the run continue
    into the gate — is deliberately not on this path.
    """
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_healing(tmp_path, change_dir, safety=safety)

    assert "fix-api" in run.order
    assert "rerun" not in run.order
    assert "complete-failed" not in run.order
    assert run.terminal == "stop"


def test_a_human_stopping_at_the_safety_interrupt_stops_healing_itself(tmp_path: Path) -> None:
    """The same hole through the human path: the fixers ran, the person said stop."""
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_healing(
        tmp_path,
        change_dir,
        safety="needs_human_review",
        resume_action="stop",
    )

    assert "safety-interrupt" in run.order
    assert "rerun" not in run.order
    assert "complete-failed" not in run.order
    assert run.terminal == "stop"


@pytest.mark.parametrize(
    ("safety", "resume_action"),
    [("stop", None), ("reject", None), ("needs_human_review", "stop")],
    ids=["refused", "rejected", "human_stop"],
)
def test_a_post_fixer_abort_never_reaches_the_gate_or_the_report(
    tmp_path: Path, safety: str, resume_action: str | None
) -> None:
    """The consequence that matters, composed across both levels.

    The healing drive supplies the child's terminal and the assurance drive replays it
    the way the runtime does — a stopped child becomes a stopped task, and a stopped
    task becomes the parent's terminal. So the parent stops on the healing node, and
    ``trace-sufficiency`` never runs: the pre-fix facts are never vouched for, and no
    report is written over them.
    """
    change_dir = _project(tmp_path, HEALTHY)
    child = _drive_healing(tmp_path, change_dir, safety=safety, resume_action=resume_action)
    assert child.terminal == "stop"

    parent = _drive_assurance(tmp_path, change_dir, subgraph_terminals={"healing": child.terminal})

    assert parent.order[-1] == "healing"
    assert GATE_NODE not in parent.order
    assert INTERRUPT_NODE not in parent.order
    assert "report" not in parent.order
    assert parent.terminal == "stop"


def test_a_safety_refusal_before_any_fixer_ran_still_completes_healing(tmp_path: Path) -> None:
    """The counterpart, so the fix is a distinction and not a blanket stop.

    ``entry`` refusing is a pre-fixer abort: nothing has touched ``tests/``, the facts
    document still describes the tests on disk, and healing therefore records ``failed``
    and returns for the gate to adjudicate normally.
    """
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive(
        tmp_path,
        change_dir,
        graph_id="healing",
        verdicts={"healing-entry-gate": "stop"},
    )

    assert "fix-api" not in run.order
    assert run.order[-1] == "complete-failed"
    assert run.terminal == "end"


# --------------------------------------------------------------------------- #
# the loop cannot be re-entered with unvalidated fixer edits
# --------------------------------------------------------------------------- #
#
# `safety-interrupt` used to offer `fix_and_proceed`, routed back to `proposal`. That
# re-entered the healing loop with the fixers' edits already written and no `rerun`
# since, and the loop has two ordinary completions that skip `rerun`:
# `proposal-eligible == false` -> `complete-skipped`, and a spent attempt budget ->
# `complete-exhausted`. Either returned to `assurance` as a normal completed healing.


def test_a_second_proposal_cannot_be_reached_from_the_safety_interrupt(tmp_path: Path) -> None:
    """The reported defect, replayed: a second attempt whose proposal has nothing eligible.

    Under the old routing this drive went ``proposal`` -> fixers -> ``safety-interrupt``
    -> ``proposal`` -> ``complete-skipped`` -> ``END``, handing ``assurance`` a completed
    healing over pre-fix facts. The action is no longer offered, and the harness feeds it
    anyway — the real runtime rejects an action outside ``interrupt.actions`` before it
    ever reaches a route — so this also pins that the route itself refuses it rather than
    relying on that check alone.
    """
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_healing(
        tmp_path,
        change_dir,
        safety="needs_human_review",
        resume_action="fix_and_proceed",
        eligible=[True, False],
    )

    assert run.order.count("proposal") == 1
    assert "complete-skipped" not in run.order
    assert "complete-exhausted" not in run.order
    assert run.terminal == "stop"


def test_no_eligible_proposal_still_completes_healing_and_is_adjudicated(tmp_path: Path) -> None:
    """The branch is not dead — it is unreachable *after a fixer*, and correct before one.

    Nothing has written to ``tests/`` on this path, so the facts document still describes
    them, and healing recording ``skipped`` and returning for the gate is exactly right.
    """
    change_dir = _project(tmp_path, HEALTHY)

    child = _drive_healing(tmp_path, change_dir, eligible=[False])

    assert "fix-api" not in child.order
    assert child.order[-1] == "complete-skipped"
    assert child.terminal == "end"

    parent = _drive_assurance(tmp_path, change_dir, subgraph_terminals={"healing": child.terminal})

    assert parent.order[-1] == "report"
    assert parent.terminal == "end"


# --------------------------------------------------------------------------- #
# the facts only reach the gate through a completed inspection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [("stop", "stop"), ("fail", "fail")],
    ids=["stopped", "failed"],
)
def test_an_inspection_that_does_not_complete_never_publishes_to_the_gate(
    tmp_path: Path, terminal: str, expected: str
) -> None:
    """``inspect-with-issues`` is a subgraph, so its writes reach the parent tree only
    when the child completes — the parent task's write-set is frozen on completion and
    on nothing else.

    The topology has to agree with that, or the gate could read a document from an
    earlier batch while believing it read this one. It does: an inspection that does
    not complete takes the parent down with it, before ``healing`` and long before the
    gate.
    """
    change_dir = _project(tmp_path, HEALTHY)

    run = _drive_assurance(tmp_path, change_dir, subgraph_terminals={"inspect-with-issues": terminal})

    assert run.order[-1] == "inspect-with-issues"
    assert "healing" not in run.order
    assert GATE_NODE not in run.order
    assert "report" not in run.order
    assert run.terminal == expected
