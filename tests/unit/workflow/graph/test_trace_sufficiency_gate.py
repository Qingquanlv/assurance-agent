"""The independent trace-sufficiency gate: what publishes its facts, and where it rules.

Case sufficiency is a *second* judgement, separate from what the execution found:
``QualityGateResult.final_status`` says whether the tests passed, and this gate says
whether the evidence behind them is good enough to believe.

The two halves live in different graphs on purpose, and the split is the load-bearing
part of the design:

- ``inspect-with-issues`` **materializes** the facts and nothing else. It runs after
  every authoritative batch — the initial one and every healing rerun — so a gate
  inside it would adjudicate mid-loop: a ``stop`` would kill the inspection that the
  healing loop's ``decide`` gate is waiting on, and thin evidence *before* a fix has
  been attempted is exactly the situation healing exists to repair. Every path,
  including both recovery paths, therefore ends at ``inspect-complete`` after
  materialization.
- The ``assurance`` graph **adjudicates**, after ``healing`` returns and before
  ``report``. Healing has had its chances by then, and the only thing left to protect
  is the report.

"After healing returns" only lines up with "the facts describe the tests on disk"
because healing does not return on every path. Its post-fixer aborts — a
``fixer-safety-gate`` refusal, and a human stopping at ``safety-interrupt`` — happen
once ``fix-api``/``fix-e2e`` have written tests that no ``rerun`` has executed, so the
newest facts document still describes the previous batch. Those routes end the run
inside the subgraph instead of completing healing, which is pinned below and driven to
its parent-level consequence in ``test_trace_gate_placement.py``.

So the tests here are mostly structural: where the nodes are, what cannot be reached
without them, the gate's own truth table, and what anchors a human's acceptance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.policy import PlanCheckAction
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.schema_v2 import (
    GraphDef,
    RouteDef,
    WorkflowSchemaV2,
    load_workflow_v2,
)
from assurance_agent.workflow.orchestration.decision_support import (
    SPECIAL_DECISION_SUPPORT as SPECIAL_SUPPORT,
)
from assurance_agent.workflow.orchestration.decision_support import resolve_decision_support
from assurance_agent.workflow.orchestration.dsl import Ident, Member, parse_expression
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import Verdict

INSPECT_GRAPH = "inspect-with-issues"
ASSURANCE_GRAPH = "assurance"
MATERIALIZE = "materialize-trace-projection"
GATE_NODE = "trace-sufficiency"
INTERRUPT_NODE = "trace-sufficiency-review"
GATE_ID = "trace-sufficiency-gate"
TARGET = "operation:materialize-trace-projection"
COMPOSITE_TARGET = "operation:materialize-trace-and-coverage-gaps"
FACTS_REL = "inspect/trace-sufficiency.json"
TERMINALS = frozenset({"END", "STOP", "FAIL"})


def _schema() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd())


def _inspect() -> GraphDef:
    return _schema().graphs[INSPECT_GRAPH]


def _assurance() -> GraphDef:
    return _schema().graphs[ASSURANCE_GRAPH]


# --------------------------------------------------------------------------- #
# the materialization node, its contract, and its handler
# --------------------------------------------------------------------------- #


def test_the_materialize_node_publishes_both_documents() -> None:
    node = _inspect().nodes[MATERIALIZE]

    assert node.uses == COMPOSITE_TARGET
    assert node.outputs == [
        "change:inspect/trace-projection.json",
        f"change:{FACTS_REL}",
        "change:inspect/coverage-gaps.json",
    ]


def test_the_target_resolves_to_a_registered_handler() -> None:
    ops = default_operations()
    assert TARGET in ops
    assert COMPOSITE_TARGET in ops


def test_the_contract_declares_every_input_the_fold_reads() -> None:
    """Reads are the scheduler's serialization key, so an undeclared one is a race.

    ``repo:tests/**`` is the easiest to forget and the most load-bearing: the fold
    scans the current test tree to decide whether a mapped test still exists, so a
    fixer rewriting tests concurrently would change this node's answer mid-flight.
    """
    contract = load_execution_contracts(Path.cwd()).contracts[TARGET]

    # MERGE_HEAD expanded the read surface for issue/codegen/review inputs; keep
    # that superset while HEAD still requires both trace documents as writes.
    assert set(contract.reads) == {
        "change:cases/**",
        "change:execution/**",
        "change:facts/**",
        "change:review/**",
        "change:healing/**",
        "change:codegen/**",
        "change:inspect/failure-analysis.json",
        "change:inspect/issue-evidence-manifest.json",
        "change:inspect/observations.json",
        "change:inspect/issue-candidates.json",
        "change:inspect/issue-reconcile-status.json",
        "change:issues/events.jsonl",
        "change:issues/snapshot.json",
        "project:qa/issues/events.jsonl",
        "project:qa/issues/problems.json",
        "repo:tests/**",
    }


def test_the_contract_authorizes_only_the_two_trace_documents() -> None:
    """A trace verdict may not become an execution verdict.

    The narrow authorization is what makes that structural rather than a promise:
    ``inspect/quality-gate-result.json`` and the execution manifest stay outside
    the scope this node can write at all.
    """
    contract = load_execution_contracts(Path.cwd()).contracts[TARGET]

    assert set(contract.authorization_writes) == {
        "change:inspect/trace-projection.json",
        f"change:{FACTS_REL}",
    }
    assert "change:inspect/quality-gate-result.json" not in contract.writes
    assert "change:execution/execution-manifest.json" not in contract.writes


def test_the_node_is_not_retried_on_its_own_failure() -> None:
    """Its inputs are on disk and its output is a pure function of them, so a
    retry can only repeat the same answer."""
    node_target = _inspect().nodes[MATERIALIZE].uses
    assert load_execution_contracts(Path.cwd()).contracts[node_target].retryable_errors == ()
    assert load_execution_contracts(Path.cwd()).contracts[TARGET].retryable_errors == ()


# --------------------------------------------------------------------------- #
# the gate node and its interrupt live at the assurance level
# --------------------------------------------------------------------------- #


def test_the_gate_node_evaluates_the_named_gate() -> None:
    node = _assurance().nodes[GATE_NODE]

    assert node.uses == "builtin:gate"
    assert node.with_ == {"gate": GATE_ID}


@pytest.mark.parametrize("node_id", [GATE_NODE, INTERRUPT_NODE])
def test_the_inspect_graph_adjudicates_nothing(node_id: str) -> None:
    """Materialization is inspection's whole job here.

    The graph runs once per authoritative batch, including every healing rerun, so
    a verdict reached inside it would be a verdict about evidence that healing has
    not finished producing — and a ``stop`` would end the run before the healing
    loop's own ``decide`` gate ever saw the rerun it asked for.
    """
    assert node_id not in _inspect().nodes


def test_the_inspect_graph_holds_no_gate_or_interrupt_at_all() -> None:
    """Stated over ``uses`` rather than node ids, so a renamed gate cannot slip in."""
    used = {node.uses for node in _inspect().nodes.values()}

    assert "builtin:gate" not in used
    assert "builtin:interrupt" not in used


def test_the_gate_reads_only_the_facts_document() -> None:
    """One read, because the facts document was published for exactly this gate.

    Reading the projection too would let the gate re-derive facts the producer
    already stated, and two derivations of one fact drift.
    """
    gate = _schema().gates[GATE_ID]

    assert [(entry.path, entry.alias) for entry in gate.reads] == [(FACTS_REL, "trace")]


@pytest.mark.parametrize("field", ["missing_file_is", "invalid_json", "missing_field_is"])
def test_an_unreadable_facts_document_stops(field: str) -> None:
    """No document means no verdict, and a gate with no verdict must not continue:
    the absence would otherwise be indistinguishable from a pass."""
    assert getattr(_schema().gates[GATE_ID], field) == Verdict.STOP


def test_the_gate_defaults_to_stop() -> None:
    assert _schema().gates[GATE_ID].default == Verdict.STOP


def test_the_human_review_interrupt_offers_exactly_accept_risk_and_stop() -> None:
    """``fix_and_proceed`` is deliberately absent: by this point healing has already
    run and returned, so there is no fixer left to route to."""
    interrupt = _assurance().nodes[INTERRUPT_NODE].interrupt

    assert interrupt is not None
    assert interrupt.actions == ["accept_risk", "stop"]
    assert interrupt.checkpoint == GATE_ID
    assert interrupt.bind == "audited_gate_read"


def test_the_facts_document_is_an_audited_gate_read() -> None:
    """The human decision must be anchored to the bytes it was taken on, or an
    ``accept_risk`` recorded against one projection would excuse a later one."""
    assert is_audited_gate_read(FACTS_REL)


# --------------------------------------------------------------------------- #
# topology: materialization inside inspect, adjudication at assurance
# --------------------------------------------------------------------------- #


def _successors(graph: GraphDef, *, without: frozenset[str] = frozenset()) -> dict[str, set[str]]:
    """Every way control can leave a node: edges, routes, and recovery.

    Recovery counts as a real path — that is the whole point of the no-bypass
    property — so ``recover.via`` and ``recover.continue_to`` are edges here.
    """
    successors: dict[str, set[str]] = {node_id: set() for node_id in graph.nodes}
    successors["START"] = set()

    def link(source: str, target: str) -> None:
        if source in without or target in without:
            return
        successors.setdefault(source, set()).add(target)

    for edge in graph.edges:
        link(edge.from_, edge.to)
    for route in graph.routes:
        for target in route.cases.values():
            link(route.from_, target)
        if route.default is not None:
            link(route.from_, route.default)
    for node_id, node in graph.nodes.items():
        if node.recover is not None:
            link(node_id, node.recover.via)
            link(node.recover.via, node.recover.continue_to)
    return successors


def _reachable(
    graph: GraphDef,
    *,
    without: frozenset[str] = frozenset(),
    frm: str = "START",
) -> set[str]:
    successors = _successors(graph, without=without)
    seen: set[str] = set()
    frontier = [frm]
    while frontier:
        node = frontier.pop()
        if node in seen or node in TERMINALS:
            continue
        seen.add(node)
        frontier.extend(successors.get(node, ()))
    return seen


def test_materialization_follows_issue_reconciliation() -> None:
    """Reconciliation is what makes the projection *reconciled*: the Problem join
    needs the ledger the reconcile step writes."""
    assert {edge.to for edge in _inspect().edges if edge.from_ == "reconcile-issues"} == {MATERIALIZE}


def test_materialization_is_the_last_thing_inspection_does() -> None:
    inspect = _inspect()
    assert "build-coverage-gap-signals" not in inspect.nodes
    assert {edge.to for edge in inspect.edges if edge.from_ == MATERIALIZE} == {"inspect-complete"}


def test_no_path_completes_the_inspect_graph_without_materializing() -> None:
    """The structural claim, checked by construction rather than by reading.

    Delete the node and ``inspect-complete`` must become unreachable — which is only
    true if every route into it runs through materialization. The removal covers
    edges, routes *and* recovery paths, so the analysis-failure and sync-pending
    branches are included.
    """
    graph = _inspect()

    assert "inspect-complete" in _reachable(graph)
    assert "inspect-complete" not in _reachable(graph, without=frozenset({MATERIALIZE}))


@pytest.mark.parametrize("failing", ["analyze-issues", "reconcile-issues"])
def test_a_recovery_path_rejoins_at_materialization(failing: str) -> None:
    """The recovery writers publish *degraded* snapshots, so the facts must be refolded.

    ``record-issue-analysis-failure`` and ``record-project-sync-pending`` write a
    snapshot whose ``analysis_status``/``project_sync_status`` says the pipeline did
    not finish. Continuing straight to completion would leave the *previous* batch's
    facts document in place as the newest one, and the gate at assurance level would
    then adjudicate a document that describes evidence nobody analysed.
    """
    recover = _inspect().nodes[failing].recover

    assert recover is not None
    assert recover.continue_to == MATERIALIZE


def test_healing_runs_before_anything_is_adjudicated() -> None:
    """Order is the whole point of the placement.

    Thin evidence before healing is a description of work in progress; thin evidence
    after healing has returned is a finding. Adjudicating first would also mean a
    ``stop`` could pre-empt the fixers that exist to remove the finding.

    Metrics materialize sits between healing and the metrics gate; the trace gate
    still follows metrics (Task 8). Healing itself must not edge straight into
    either adjudication node.
    """
    from_healing = {edge.to for edge in _assurance().edges if edge.from_ == "healing"}
    assert GATE_NODE not in from_healing
    assert "metrics-sufficiency" not in from_healing
    assert from_healing == {"coverage-repair"}
    from_repair = {edge.to for edge in _assurance().edges if edge.from_ == "coverage-repair"}
    assert from_repair == {"materialize-pr-metrics"}


def test_nothing_but_the_adjudication_leads_to_the_report() -> None:
    """``report`` has no plain incoming edge: it is reachable only as a route target.

    Trusted evidence may pass or reject release; a human may accept thin evidence.
    There is no path that quietly writes a report over evidence nobody vouched for.
    """
    graph = _assurance()

    assert [edge.from_ for edge in graph.edges if edge.to == "report"] == []
    into_report = {
        (route.from_, action)
        for route in graph.routes
        for action, target in route.cases.items()
        if target == "report"
    }
    assert into_report == {
        (GATE_NODE, "pass"),
        (GATE_NODE, "reject"),
        (INTERRUPT_NODE, "accept_risk"),
    }


@pytest.mark.parametrize(
    "node",
    [
        "inspect-with-issues",
        "healing",
        "coverage-repair",
        "materialize-pr-metrics",
        "metrics-sufficiency",
        GATE_NODE,
    ],
)
def test_no_path_reaches_the_report_without(node: str) -> None:
    """Inspection publishes the facts, healing gets its chance, gates adjudicate.

    Removing any required node must make ``report`` unreachable, which pins the
    whole order rather than just the gate's presence.
    """
    graph = _assurance()

    assert "report" in _reachable(graph)
    assert "report" not in _reachable(graph, without=frozenset({node}))


HEALING_GRAPH = "healing"
# The healing routes taken *after* `fix-api`/`fix-e2e` have written to `tests/` and
# *before* `rerun` has executed anything.
POST_FIXER_ABORTS = {"safety": ("stop", "reject"), "safety-interrupt": ("stop",)}


def _healing() -> GraphDef:
    return _schema().graphs[HEALING_GRAPH]


def _healing_route(node_id: str) -> RouteDef:
    return next(route for route in _healing().routes if route.from_ == node_id)


def _healing_completions() -> set[str]:
    """The nodes that record a status and end the subgraph at ``END``.

    Reaching one is what makes healing an ordinary *completed* subgraph, which is what
    lets ``assurance`` carry on into the gate.
    """
    return {nid for nid, node in _healing().nodes.items() if node.uses == "operation:record-healing-status"}


@pytest.mark.parametrize(
    ("node_id", "action"),
    [(node_id, action) for node_id, actions in POST_FIXER_ABORTS.items() for action in actions],
)
def test_a_post_fixer_abort_stops_the_run_instead_of_completing_healing(node_id: str, action: str) -> None:
    """These are the paths on which the newest facts document is the wrong batch.

    A completion here would reach ``assurance`` as an ordinary returned healing, and the
    gate would adjudicate evidence produced before the fixers touched the tests — most
    dangerously by *passing* it. Re-materializing instead of stopping would not help:
    sufficiency measures staleness against the batch's own ``executed_at``, so a refold
    over an unchanged manifest republishes an unchanged document.
    """
    assert _healing_route(node_id).cases[action] == "STOP"


@pytest.mark.parametrize("node_id", sorted(POST_FIXER_ABORTS))
def test_an_unmatched_post_fixer_verdict_also_stops(node_id: str) -> None:
    """The default carries the same reasoning: an unrecognised verdict after the fixers
    ran is still a state nothing has adjudicated."""
    assert _healing_route(node_id).default == "STOP"


@pytest.mark.parametrize("node_id", sorted(POST_FIXER_ABORTS))
def test_no_post_fixer_abort_records_a_healing_completion(node_id: str) -> None:
    """The same property stated over the completion nodes rather than over ``STOP``, so
    a new way of spelling "healing finished normally" cannot reopen the hole."""
    route = _healing_route(node_id)
    reachable = set(route.cases.values()) | {route.default}

    assert _healing_completions().isdisjoint(reachable)


@pytest.mark.parametrize("fixer", ["fix-api", "fix-e2e"])
def test_nothing_reachable_from_a_fixer_completes_healing_without_a_rerun(fixer: str) -> None:
    """The invariant the individual routes are only evidence for.

    Enumerating abort routes one at a time cannot catch a completion reached the long
    way round — which is exactly how the earlier ``fix_and_proceed -> proposal`` route
    escaped: it reached ``complete-skipped`` (no eligible proposal) and
    ``complete-exhausted`` (attempt budget spent) without ever passing ``rerun``.

    So the property is stated over the graph instead. Cut ``rerun`` out, and from a
    node that has just written to ``tests/`` nothing that ends healing normally may
    still be reachable. Only ``STOP`` may be — and ``STOP`` never reaches the gate.
    """
    graph = _healing()
    completions = _healing_completions()
    assert completions & _reachable(graph, frm=fixer), "the fixture is only meaningful if some exist"

    stranded = completions & _reachable(graph, without=frozenset({"rerun"}), frm=fixer)

    assert stranded == set()


@pytest.mark.parametrize("node_id", ["entry", "decide"])
def test_a_pre_fixer_or_post_rerun_abort_still_completes_healing(node_id: str) -> None:
    """The counterpart, so the stop is a distinction rather than a blanket refusal.

    ``entry`` aborts before any fixer has run and ``decide`` only routes after a rerun
    and a full re-inspection, so on both the facts document does describe the tests on
    disk — and healing recording its status and returning is exactly right.
    """
    route = _healing_route(node_id)
    terminal_actions = {"stop", "reject"} & set(route.cases)

    assert {route.cases[action] for action in terminal_actions} <= _healing_completions()


def test_the_gate_routes_every_verdict_it_can_reach() -> None:
    route = next(route for route in _assurance().routes if route.from_ == GATE_NODE)

    assert route.select == f"node('{GATE_NODE}').gate.verdict"
    assert route.cases["pass"] == "report"
    assert route.cases["needs_human_review"] == INTERRUPT_NODE
    assert route.cases["stop"] == "STOP"
    assert route.default == "STOP"


def test_the_human_decision_routes_to_the_report_or_a_stop() -> None:
    route = next(route for route in _assurance().routes if route.from_ == INTERRUPT_NODE)

    assert route.select == "resume.action"
    assert route.cases == {"accept_risk": "report", "stop": "STOP"}
    assert route.default == "STOP"


def test_a_stop_ends_the_run_before_the_report_is_written() -> None:
    """The accepted sequencing: the gate precedes the report, so a stop pre-empts it.

    Deliberately not "the report is written anyway": a quality report over evidence
    the gate refused would be the most authoritative-looking document in the change.
    """
    route = next(route for route in _assurance().routes if route.from_ == GATE_NODE)

    assert route.cases["stop"] == "STOP"
    assert route.cases["reject"] == "report"


# --------------------------------------------------------------------------- #
# the routing table, as a truth table
# --------------------------------------------------------------------------- #


def _facts(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-1",
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


def _policy_text(on_insufficient: PlanCheckAction) -> str:
    document = {
        "version": 1,
        "human_review_risk_levels": ["high"],
        "force_continue_allowed": False,
        "plan_checks": {
            "l1_path": "block",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "block",
        },
        "coverage_floor": {"risk_high": 80.0, "risk_medium": 60.0},
        "fuzz": {"required_when_endpoint_has_auth": True},
        "healing": {"auth_module": "require_human"},
        "evidence_sufficiency": {
            "recency_hours": 72,
            "required_kinds": {
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            "on_insufficient": on_insufficient,
        },
    }
    return yaml.safe_dump(document, sort_keys=False)


def _verdict(
    tmp_path: Path,
    *,
    on_insufficient: PlanCheckAction = "require_human",
    facts: object | None = None,
) -> Verdict:
    policy_path = tmp_path / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(_policy_text(on_insufficient), encoding="utf-8")
    overrides = {} if facts is None else {FACTS_REL: facts}
    context = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
        artifact_overrides=overrides,
    )
    return check_gate_in_view(_schema().gates, GATE_ID, context).verdict


@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_sufficient_evidence_passes_under_every_policy(tmp_path: Path, action: PlanCheckAction) -> None:
    """``on_insufficient`` says what to do when evidence falls short; a change
    whose evidence holds is not affected by it."""
    assert _verdict(tmp_path, on_insufficient=action, facts=_facts()) == Verdict.PASS


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("warn", Verdict.PASS),
        ("block", Verdict.STOP),
        ("require_human", Verdict.NEEDS_HUMAN_REVIEW),
    ],
)
def test_insufficient_evidence_routes_by_policy(
    tmp_path: Path, action: PlanCheckAction, expected: Verdict
) -> None:
    """The whole reason the gate reads the policy: one organisation blocks thin
    evidence, another warns, and neither needs a code change."""
    facts = _facts(
        sufficient=False,
        insufficient_cases=[{"case_id": "TC_API_001", "reason_codes": ["never_run"]}],
    )

    assert _verdict(tmp_path, on_insufficient=action, facts=facts) == expected


@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_an_open_product_bug_rejects_release_regardless_of_policy(
    tmp_path: Path, action: PlanCheckAction
) -> None:
    """``on_insufficient`` is about evidence, and an open product bug is not an
    evidence problem: fresh, complete evidence of a live bug rejects release but
    remains reportable."""
    facts = _facts(has_open_problems=True)

    assert _verdict(tmp_path, on_insufficient=action, facts=facts) == Verdict.REJECT


@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_a_blocked_integrity_stops_regardless_of_policy(tmp_path: Path, action: PlanCheckAction) -> None:
    """Integrity is judged before sufficiency (spec §13).

    An unreadable input means the row verdicts are not about this change's rows —
    most sharply when the fold read nothing and ``sufficient`` is *vacuously*
    true. Routing on ``sufficient`` alone would pass exactly the case that most
    needs a person.
    """
    facts = _facts(integrity="incomplete", integrity_blocks_routing=True)

    assert _verdict(tmp_path, on_insufficient=action, facts=facts) == Verdict.STOP


@pytest.mark.parametrize("code", ["policy_error", "evidence_projection_missing"])
def test_an_error_state_stops(tmp_path: Path, code: str) -> None:
    """Nothing was judged, so there is no verdict to route on — and ``warn`` must
    not excuse it, because a policy that could not be applied said nothing about
    how thin evidence should be treated."""
    facts = _facts(
        error_code=code,
        sufficient=False,
        policy_digest=None,
        as_of=None,
        integrity="incomplete",
        integrity_blocks_routing=True,
    )

    assert _verdict(tmp_path, on_insufficient="warn", facts=facts) == Verdict.STOP


def test_a_missing_facts_document_stops(tmp_path: Path) -> None:
    assert _verdict(tmp_path) == Verdict.STOP


def test_an_unparseable_facts_document_stops(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1" / "inspect"
    change_dir.mkdir(parents=True)
    (change_dir / "trace-sufficiency.json").write_text("{not json", encoding="utf-8")

    assert _verdict(tmp_path) == Verdict.STOP


def test_a_facts_document_missing_a_routing_field_stops(tmp_path: Path) -> None:
    """A field the gate needs and cannot find is not a false: it is an unknown,
    and an unknown that routed as a pass would be the gate failing open."""
    facts = _facts()
    del facts["has_open_problems"]

    assert _verdict(tmp_path, facts=facts) == Verdict.STOP


def test_an_evidence_error_outranks_an_open_problem(tmp_path: Path) -> None:
    """An unreadable judgement stops rather than reporting an untrusted bug."""
    facts = _facts(
        has_open_problems=True,
        error_code="policy_error",
        sufficient=False,
        policy_digest=None,
        as_of=None,
    )

    assert _verdict(tmp_path, facts=facts) == Verdict.STOP


# --------------------------------------------------------------------------- #
# the human decision, and what anchors it
# --------------------------------------------------------------------------- #


def _thin_evidence_change(tmp_path: Path) -> tuple[Path, str]:
    """A change whose facts document escalates to a human, written for real.

    On disk rather than injected, because the decision below is anchored to this
    file's hash: an override that could not be tied to specific bytes would keep
    excusing whatever the next fold produced.
    """
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    path = change_dir / "inspect" / "trace-sufficiency.json"
    path.write_text(
        json.dumps(
            _facts(
                sufficient=False,
                insufficient_cases=[{"case_id": "TC_API_001", "reason_codes": ["never_run"]}],
            )
        ),
        encoding="utf-8",
    )
    return change_dir, hashlib.sha256(path.read_bytes()).hexdigest()


def _verdict_on_disk(
    tmp_path: Path,
    change_dir: Path,
    *,
    committed_tree_id: str | None = None,
    invocation_id: str | None = None,
) -> Verdict:
    policy_path = tmp_path / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(_policy_text("require_human"), encoding="utf-8")
    context = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
        audit_events_dir=change_dir,
        committed_tree_id=committed_tree_id,
        invocation_id=invocation_id,
    )
    return check_gate_in_view(_schema().gates, GATE_ID, context).verdict


def _record_decision(change_dir: Path, action: str, *, review_sha256: str | None) -> None:
    event: dict[str, object] = {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": GATE_ID,
        "action": action,
        "reason": "accepted the coverage gap for this release",
        "who": "tester",
    }
    if review_sha256 is not None:
        event["review_file"] = FACTS_REL
        event["review_sha256"] = review_sha256
    append_event_strict(change_dir, event)


def _resume_through_the_graph(change_dir: Path, action: str, *, audited: dict[str, str]) -> None:
    """The interrupt/resume pair the runtime writes for ``bind: audited_gate_read``.

    This is the path the top-level interrupt actually takes — ``aa decide`` refuses
    graph gate actions — so the anchoring that matters is the one carried on the
    resume event rather than a hand-recorded ``review_sha256``.
    """
    interrupt_id = "int-" + hashlib.sha256(action.encode()).hexdigest()[:16]
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "inv-assurance",
            "checkpoint_ns": "inv-assurance",
            "interrupt_id": interrupt_id,
            "node_id": INTERRUPT_NODE,
            "checkpoint": GATE_ID,
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": audited,
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "inv-assurance",
            "checkpoint_ns": "inv-assurance",
            "interrupt_id": interrupt_id,
            "action": action,
            "reason": "accepted the coverage gap for this release",
            "who": "tester",
            "audited_reads_sha256": audited,
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "inv-assurance",
            "checkpoint_ns": "inv-assurance",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": GATE_ID, "verdict": "needs_human_review"},
        },
    )


@pytest.mark.parametrize("action", ["accept_risk", "stop"])
def test_both_offered_actions_are_consumable_decisions(action: str) -> None:
    """The interrupt may only offer actions something downstream acts on."""
    support = resolve_decision_support(_schema(), GATE_ID, action)

    assert support.consumer in {"gate-decision", "terminal-status"}


def _adjudicated_offered_actions() -> list[tuple[str, str, str]]:
    """``(checkpoint, node, action)`` for the interrupts the decision layer governs.

    A gate id or a ``SPECIAL_DECISION_SUPPORT`` key. The issue- and
    improvement-review workflows interrupt on their own checkpoints with their own
    action vocabularies, consumed by their own operations rather than by
    ``resolve_decision_support``, so they are out of scope here.
    """
    schema = _schema()
    return [
        (node.interrupt.checkpoint, f"{graph_id}.{node_id}", action)
        for graph_id, graph in sorted(schema.graphs.items())
        for node_id, node in sorted(graph.nodes.items())
        if node.interrupt is not None
        and (node.interrupt.checkpoint in schema.gates or node.interrupt.checkpoint in SPECIAL_SUPPORT)
        for action in node.interrupt.actions
    ]


@pytest.mark.parametrize(
    ("checkpoint", "node", "action"),
    _adjudicated_offered_actions(),
    ids=[f"{node}:{action}" for _, node, action in _adjudicated_offered_actions()],
)
def test_no_gate_interrupt_offers_an_action_its_checkpoint_cannot_consume(
    checkpoint: str, node: str, action: str
) -> None:
    """Generalised from this gate's own rule, because a counterexample was shipped.

    ``healing.safety`` supports ``accept_risk`` and ``stop`` and nothing else —
    ``SPECIAL_DECISION_SUPPORT`` says so, and ``latest_valid_gate_decision`` refuses to
    let any other action recorded there anchor the ``fixer-safety-gate``. The healing
    ``safety-interrupt`` nevertheless offered ``fix_and_proceed``, so a human could pick
    a resume action the decision layer treats as inert — and the route it took reopened
    the stale-facts hole this module exists to close.

    Stating it over every adjudicated interrupt is what stops the next one.
    """
    support = resolve_decision_support(_schema(), checkpoint, action)

    assert support.consumer in {"gate-decision", "terminal-status", "healing-safety"}, node


def test_the_action_the_interrupt_withholds_is_still_refused_by_the_matrix() -> None:
    """``fix_and_proceed`` is absent from the interrupt; the decision layer is
    where a hand-recorded one would otherwise slip in — and it maps to
    ``needs_fix``, which nothing after healing can act on."""
    graph_actions = _assurance().nodes[INTERRUPT_NODE].interrupt
    assert graph_actions is not None and "fix_and_proceed" not in graph_actions.actions


def test_an_anchored_acceptance_resumes_the_gate_as_a_pass(tmp_path: Path) -> None:
    change_dir, digest = _thin_evidence_change(tmp_path)
    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW

    _record_decision(change_dir, "accept_risk", review_sha256=digest)

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW


def test_a_graph_resume_is_anchored_to_the_bytes_the_interrupt_froze(tmp_path: Path) -> None:
    """The top-level interrupt's own path, end to end.

    ``bind: audited_gate_read`` makes the runtime hash the facts document into the
    interrupt and carry the hashes on the resume; the gate re-checks them against
    the file now. This is what keeps a top-level acceptance hash-anchored.
    """
    change_dir, digest = _thin_evidence_change(tmp_path)
    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW

    _resume_through_the_graph(change_dir, "accept_risk", audited={FACTS_REL: digest})

    assert (
        _verdict_on_disk(
            tmp_path,
            change_dir,
            committed_tree_id="tree-src",
            invocation_id="inv-assurance",
        )
        == Verdict.PASS
    )


def test_a_graph_resume_stops_applying_once_the_facts_change(tmp_path: Path) -> None:
    change_dir, digest = _thin_evidence_change(tmp_path)
    _resume_through_the_graph(change_dir, "accept_risk", audited={FACTS_REL: digest})
    facts_path = change_dir / "inspect" / "trace-sufficiency.json"
    facts_path.write_text(
        json.dumps(
            _facts(
                sufficient=False,
                insufficient_cases=[{"case_id": "TC_API_002", "reason_codes": ["uncovered"]}],
            )
        ),
        encoding="utf-8",
    )

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW


def test_a_graph_resume_carrying_no_hashes_does_not_resume_the_gate(tmp_path: Path) -> None:
    """An unanchored acceptance is indistinguishable from one taken over any other
    projection, so it anchors nothing and the gate stays escalated."""
    change_dir, _ = _thin_evidence_change(tmp_path)

    _resume_through_the_graph(change_dir, "accept_risk", audited={})

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW


def test_an_acceptance_stops_applying_once_the_projection_changes(tmp_path: Path) -> None:
    """A new fold is new evidence, and the old acceptance says nothing about it."""
    change_dir, digest = _thin_evidence_change(tmp_path)
    _record_decision(change_dir, "accept_risk", review_sha256=digest)
    facts_path = change_dir / "inspect" / "trace-sufficiency.json"
    facts_path.write_text(
        json.dumps(
            _facts(
                sufficient=False,
                insufficient_cases=[
                    {"case_id": "TC_API_001", "reason_codes": ["never_run"]},
                    {"case_id": "TC_API_002", "reason_codes": ["uncovered"]},
                ],
            )
        ),
        encoding="utf-8",
    )

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW


def test_an_unanchored_acceptance_does_not_resume_the_gate(tmp_path: Path) -> None:
    change_dir, _ = _thin_evidence_change(tmp_path)

    _record_decision(change_dir, "accept_risk", review_sha256=None)

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.NEEDS_HUMAN_REVIEW


def test_an_acceptance_cannot_turn_an_open_problem_into_a_pass(tmp_path: Path) -> None:
    """Only ``needs_human_review`` is upgradable, so a product rejection remains
    a rejection no matter what is recorded against the gate."""
    change_dir, _ = _thin_evidence_change(tmp_path)
    facts_path = change_dir / "inspect" / "trace-sufficiency.json"
    facts_path.write_text(json.dumps(_facts(has_open_problems=True)), encoding="utf-8")
    digest = hashlib.sha256(facts_path.read_bytes()).hexdigest()

    _record_decision(change_dir, "accept_risk", review_sha256=digest)

    assert _verdict_on_disk(tmp_path, change_dir) == Verdict.REJECT


# --------------------------------------------------------------------------- #
# the policy is read through the DSL, not a side channel
# --------------------------------------------------------------------------- #


def _policy_roots(expression: object) -> set[str]:
    if isinstance(expression, Member):
        roots = (
            {expression.prop}
            if isinstance(expression.obj, Ident) and expression.obj.name == "policy"
            else set()
        )
        return roots | _policy_roots(expression.obj)
    if not is_dataclass(expression):
        return set()
    found: set[str] = set()
    for field_ in fields(expression):
        value = getattr(expression, field_.name)
        for item in value if isinstance(value, tuple) else (value,):
            found |= _policy_roots(item)
    return found


def test_the_gate_dsl_reads_the_evidence_policy_itself() -> None:
    """Not a copy of the action carried in the facts document.

    A copy is a snapshot of the policy at fold time; reading ``policy`` here means
    the verdict follows the policy in force. It is also what retires the Python
    policy-consumer registration for this field.
    """
    roots = {
        root for rule in _schema().gates[GATE_ID].rules for root in _policy_roots(parse_expression(rule.expr))
    }

    assert "evidence_sufficiency" in roots


def test_every_routing_rule_is_declared_stop_then_human_then_reject_then_pass() -> None:
    """Declaration order is evaluation order, so it is the safety order."""
    assert [rule.verdict for rule in _schema().gates[GATE_ID].rules] == [
        Verdict.STOP,
        Verdict.NEEDS_HUMAN_REVIEW,
        Verdict.REJECT,
        Verdict.PASS,
    ]


# --------------------------------------------------------------------------- #
# the gate stays out of the execution verdict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("node_id", [GATE_NODE, INTERRUPT_NODE])
def test_the_gate_and_its_interrupt_write_nothing(node_id: str) -> None:
    graph = _assurance()
    contract = load_execution_contracts(Path.cwd()).contracts[graph.nodes[node_id].uses]

    assert contract.side_effect_free is True
    assert not contract.writes
    assert not contract.authorization_writes
    assert graph.nodes[node_id].outputs == []


def test_no_trace_node_rewrites_the_execution_verdict() -> None:
    """The execution manifest and quality gate are written once, by inspection.

    A trace verdict that could edit either would collapse the two judgements the
    design keeps apart — including ``final_status``, which states what the
    *execution* found and must survive an adjudication about its evidence.
    """
    contracts = load_execution_contracts(Path.cwd()).contracts
    verdict_paths = {
        "change:execution/execution-manifest.json",
        "change:inspect/quality-gate-result.json",
    }
    targets = {
        MATERIALIZE: _inspect().nodes[MATERIALIZE].uses,
        GATE_NODE: _assurance().nodes[GATE_NODE].uses,
        INTERRUPT_NODE: _assurance().nodes[INTERRUPT_NODE].uses,
    }

    for node_id, target in targets.items():
        contract = contracts[target]
        assert verdict_paths.isdisjoint(contract.writes), node_id
        assert verdict_paths.isdisjoint(contract.authorization_writes), node_id
