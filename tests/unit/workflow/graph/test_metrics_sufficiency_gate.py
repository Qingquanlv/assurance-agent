"""The independent metrics-sufficiency gate: Python evaluator, placement, isolation.

Metrics shortboards are a *second* judgement from execution ``final_status``:
``QualityGateResult.final_status`` / ``execution-manifest.final_status`` say whether
tests passed; this gate says whether the PR metric vector clears floors / has no
collection gaps. Healing-loop reject must never read metrics. ``archive-gate``
*does* read metrics (only) to block archival on a collection gap — see
``test_archive_gate_blocks_on_metrics_collection_gap`` below.

Adjudication is ``evaluate_metrics_sufficiency`` (Task 2) — not a tautological DSL
restating floors. The packaged gate has no ``*_when`` rules; the runtime calls the
evaluator. Routing table (module docstring of ``metrics_sufficiency``):

1. collection gaps → reject (blocks archive, keeps flowing to report/retro)
2. evaluated boolean floor miss → stop
3. empty cadence → skip
4. numeric miss by ``on_insufficient`` (require_human / warn / block)
5. else pass

Placement (Task 8): after healing (+ materialize-pr-metrics), before report —
same post-fixer STOP discipline as the trace gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.metrics import (
    MetricEntry,
    MetricScope,
    MetricsDocument,
)
from assurance_agent.artifacts.models.policy import MetricCadenceSchedule, MetricFloor
from assurance_agent.artifacts.policy import load_policy_bytes
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2, load_workflow_v2
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import Verdict

ASSURANCE_GRAPH = "assurance"
HEALING_GRAPH = "healing"
MATERIALIZE = "materialize-pr-metrics"
GATE_NODE = "metrics-sufficiency"
INTERRUPT_NODE = "metrics-sufficiency-review"
TRACE_GATE_NODE = "trace-sufficiency"
GATE_ID = "metrics-sufficiency-gate"
COLLECT_TARGET = "operation:collect-pr-metrics-batch"
COMPOSITE_TARGET = "operation:run-tests-and-collect-pr-metrics"
MATERIALIZE_TARGET = "operation:materialize-pr-metrics"
METRICS_REL = "inspect/metrics.json"
TERMINALS = frozenset({"END", "STOP", "FAIL"})


def _schema() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd())


def _assurance() -> GraphDef:
    return _schema().graphs[ASSURANCE_GRAPH]


def _healing() -> GraphDef:
    return _schema().graphs[HEALING_GRAPH]


def _document(**overrides: object) -> MetricsDocument:
    payload: dict[str, object] = {
        "schema_version": "2",
        "change_id": "CH-1",
        "cadence": "pr",
        "computed_at": datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
        "risk_tier": "low",
        "risk_tier_lower_bound": "low",
        "risk_tier_declared": None,
        "risk_declaration_lowered": False,
        "risk_lowered_declarations": (),
        "metrics": {
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=0.9,
                declared=MetricScope.of(total=10, covered=9),
                evidence="constraint-coverage.json",
            )
        },
        "policy_digest": "0" * 64,
    }
    payload.update(overrides)
    return MetricsDocument.model_validate(payload)


def _project(
    tmp_path: Path, document: MetricsDocument | None, *, on_insufficient: str = "require_human"
) -> Path:
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["on_insufficient"] = on_insufficient
    (tmp_path / ".aa" / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    if document is not None:
        (change_dir / METRICS_REL).write_text(
            document.model_dump_json(indent=2),
            encoding="utf-8",
        )
    return change_dir


def _check(tmp_path: Path, change_dir: Path) -> str:
    report = check_gate_in_view(
        _schema().gates,
        GATE_ID,
        GateEvaluationContext(
            project_root=tmp_path,
            repo_root=tmp_path,
            change_dir=change_dir,
            change_id="CH-1",
            params={},
            state_values={},
            node_results={},
        ),
    )
    return report.verdict.value


# --------------------------------------------------------------------------- #
# collect + materialize nodes
# --------------------------------------------------------------------------- #


def test_assurance_collects_pr_metrics_after_execution() -> None:
    node = _assurance().nodes["execution"]
    assert node.uses == COMPOSITE_TARGET
    edge_pairs = {(e.from_, e.to) for e in _assurance().edges}
    assert ("execution", "inspect-with-issues") in edge_pairs
    assert "collect-pr-metrics-batch" not in _assurance().nodes


def test_healing_collects_pr_metrics_after_rerun() -> None:
    node = _healing().nodes["rerun"]
    assert node.uses == COMPOSITE_TARGET
    edge_pairs = {(e.from_, e.to) for e in _healing().edges}
    assert ("rerun", "inspect-with-issues") in edge_pairs
    assert "collect-pr-metrics-batch" not in _healing().nodes


def test_materialize_runs_after_healing_before_the_metrics_gate() -> None:
    node = _assurance().nodes[MATERIALIZE]
    assert node.uses == MATERIALIZE_TARGET
    edge_pairs = {(e.from_, e.to) for e in _assurance().edges}
    assert ("healing", "coverage-repair") in edge_pairs
    assert ("coverage-repair", MATERIALIZE) in edge_pairs
    assert ("healing", MATERIALIZE) not in edge_pairs
    assert (MATERIALIZE, GATE_NODE) in edge_pairs


def test_collect_and_materialize_resolve_to_registered_handlers() -> None:
    ops = default_operations()
    assert COLLECT_TARGET in ops
    assert COMPOSITE_TARGET in ops
    assert MATERIALIZE_TARGET in ops


def test_materialize_contract_authorizes_inspect_metrics_only() -> None:
    contract = load_execution_contracts(Path.cwd()).contracts[MATERIALIZE_TARGET]
    assert "change:inspect/metrics.json" in contract.authorization_writes
    assert "change:execution/execution-manifest.json" not in contract.writes
    assert "change:inspect/quality-gate-result.json" not in contract.writes


# --------------------------------------------------------------------------- #
# gate node + interrupt at assurance level
# --------------------------------------------------------------------------- #


def test_the_gate_node_evaluates_the_named_gate() -> None:
    node = _assurance().nodes[GATE_NODE]
    assert node.uses == "builtin:gate"
    assert node.with_ == {"gate": GATE_ID}


def test_the_gate_has_no_tautological_dsl_rules() -> None:
    """Floors/cadence live in ``evaluate_metrics_sufficiency``, not DSL."""
    gate = _schema().gates[GATE_ID]
    assert gate.rules == []
    assert [(entry.path, entry.alias) for entry in gate.reads] == [(METRICS_REL, "metrics")]


@pytest.mark.parametrize("field", ["missing_file_is", "invalid_json", "missing_field_is"])
def test_an_unreadable_metrics_document_stops(field: str) -> None:
    assert getattr(_schema().gates[GATE_ID], field) == Verdict.STOP


def test_the_human_review_interrupt_offers_exactly_accept_risk_and_stop() -> None:
    interrupt = _assurance().nodes[INTERRUPT_NODE].interrupt
    assert interrupt is not None
    assert interrupt.actions == ["accept_risk", "stop"]
    assert interrupt.checkpoint == GATE_ID
    assert interrupt.bind == "audited_gate_read"


def test_the_metrics_document_is_an_audited_gate_read() -> None:
    assert is_audited_gate_read(METRICS_REL)


def test_inspect_graph_holds_no_metrics_adjudication() -> None:
    inspect = _schema().graphs["inspect-with-issues"]
    assert GATE_NODE not in inspect.nodes
    assert INTERRUPT_NODE not in inspect.nodes
    assert MATERIALIZE not in inspect.nodes


# --------------------------------------------------------------------------- #
# topology spine
# --------------------------------------------------------------------------- #


def _successors(graph: GraphDef) -> dict[str, set[str]]:
    successors: dict[str, set[str]] = {node_id: set() for node_id in graph.nodes}
    successors["START"] = set()

    def link(source: str, target: str) -> None:
        successors.setdefault(source, set()).add(target)

    for edge in graph.edges:
        link(edge.from_, edge.to)
    for route in graph.routes:
        for target in route.cases.values():
            link(route.from_, target)
        if route.default is not None:
            link(route.from_, route.default)
    return successors


def _reachable(graph: GraphDef, *, without: frozenset[str] = frozenset(), frm: str = "START") -> set[str]:
    successors = _successors(graph)
    for cut in without:
        successors.pop(cut, None)
        for targets in successors.values():
            targets.discard(cut)
    seen: set[str] = set()
    frontier = [frm]
    while frontier:
        node = frontier.pop()
        if node in seen or node in TERMINALS:
            continue
        seen.add(node)
        frontier.extend(successors.get(node, ()))
    return seen


def test_assurance_spine_is_collect_inspect_heal_materialize_metrics_trace_report() -> None:
    """Task 8 path + coverage-repair, with the existing trace gate preserved before report."""
    edge_pairs = {(e.from_, e.to) for e in _assurance().edges}
    assert ("execution", "inspect-with-issues") in edge_pairs
    assert ("inspect-with-issues", "healing") in edge_pairs
    assert ("healing", "coverage-repair") in edge_pairs
    assert ("coverage-repair", MATERIALIZE) in edge_pairs
    assert ("healing", MATERIALIZE) not in edge_pairs
    assert (MATERIALIZE, GATE_NODE) in edge_pairs
    assert ("healing", "report") not in edge_pairs
    assert (GATE_NODE, "report") not in edge_pairs  # only via routes


def test_metrics_gate_routes_every_verdict_it_can_reach() -> None:
    route = next(route for route in _assurance().routes if route.from_ == GATE_NODE)
    assert route.select == f"node('{GATE_NODE}').gate.verdict"
    assert route.cases["pass"] == TRACE_GATE_NODE
    assert route.cases["skip"] == TRACE_GATE_NODE
    assert route.cases["needs_human_review"] == INTERRUPT_NODE
    assert route.cases["stop"] == "STOP"
    assert route.cases["reject"] == TRACE_GATE_NODE
    assert route.default == "STOP"


def test_metrics_review_accept_risk_continues_to_trace_gate() -> None:
    route = next(route for route in _assurance().routes if route.from_ == INTERRUPT_NODE)
    assert route.cases == {"accept_risk": TRACE_GATE_NODE, "stop": "STOP"}


@pytest.mark.parametrize("node", ["healing", "coverage-repair", MATERIALIZE, GATE_NODE, TRACE_GATE_NODE])
def test_no_path_reaches_the_report_without(node: str) -> None:
    graph = _assurance()
    assert "report" in _reachable(graph)
    assert "report" not in _reachable(graph, without=frozenset({node}))


# --------------------------------------------------------------------------- #
# truth table — real evaluator through the packaged gate
# --------------------------------------------------------------------------- #


def test_healthy_pr_metrics_pass(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, _document())
    assert _check(tmp_path, change_dir) == "pass"


def test_collection_gaps_reject(tmp_path: Path) -> None:
    from assurance_agent.artifacts.models.metrics import MetricCollectionGap

    doc = _document(
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="collection_failed",
                value=None,
                declared=None,
                evidence="",
            )
        },
        collection_gaps=(
            MetricCollectionGap(code="collection_failed", detail="boom", metric="constraint_coverage"),
        ),
    )
    change_dir = _project(tmp_path, doc)
    assert _check(tmp_path, change_dir) == "reject"
    assert evaluate_metrics_sufficiency(
        doc, load_policy_bytes(None, origin="packaged").evidence_sufficiency
    ).verdict == ("reject")


def test_block_floor_stops(tmp_path: Path) -> None:
    sufficiency = load_policy_bytes(None, origin="packaged").evidence_sufficiency
    raised = {tier: dict(band) for tier, band in sufficiency.floors.items()}
    raised["low"] = {**raised["low"], "constraint_coverage": MetricFloor(target="value", min=1.0)}
    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["floors"] = {
        tier: {k: v.model_dump(mode="json") for k, v in band.items()} for tier, band in raised.items()
    }
    policy["evidence_sufficiency"]["on_insufficient"] = "block"
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aa" / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    doc = _document(
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=0.5,
                declared=MetricScope.of(total=2, covered=1),
                evidence="constraint-coverage.json",
            )
        }
    )
    (change_dir / METRICS_REL).write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    assert _check(tmp_path, change_dir) == "stop"


def test_require_human_shortboard_needs_human(tmp_path: Path) -> None:
    sufficiency = load_policy_bytes(None, origin="packaged").evidence_sufficiency
    raised = {tier: dict(band) for tier, band in sufficiency.floors.items()}
    raised["low"] = {**raised["low"], "constraint_coverage": MetricFloor(target="value", min=1.0)}
    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["floors"] = {
        tier: {k: v.model_dump(mode="json") for k, v in band.items()} for tier, band in raised.items()
    }
    policy["evidence_sufficiency"]["on_insufficient"] = "require_human"
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aa" / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    doc = _document(
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=0.5,
                declared=MetricScope.of(total=2, covered=1),
                evidence="constraint-coverage.json",
            )
        }
    )
    (change_dir / METRICS_REL).write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    assert _check(tmp_path, change_dir) == "needs_human_review"


def test_empty_pr_cadence_skips(tmp_path: Path) -> None:
    policy = load_policy_bytes(None, origin="packaged").model_dump(mode="json")
    policy["evidence_sufficiency"]["cadence"] = MetricCadenceSchedule(
        pr=[], nightly=["mutation_score"]
    ).model_dump(mode="json")
    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aa" / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    (change_dir / METRICS_REL).write_text(_document().model_dump_json(indent=2), encoding="utf-8")
    assert _check(tmp_path, change_dir) == "skip"


def test_missing_metrics_document_stops(tmp_path: Path) -> None:
    change_dir = _project(tmp_path, None)
    assert _check(tmp_path, change_dir) == "stop"


def test_nightly_not_evaluated_entries_do_not_enter_ratio() -> None:
    """M1: nightly keys stay not_evaluated; combined PR+nightly floor_ratio is M2."""
    from assurance_agent.artifacts.models.metrics import MetricShortboard

    doc = _document(
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=0.9,
                declared=MetricScope.of(total=10, covered=9),
                evidence="constraint-coverage.json",
            ),
            "mutation_score": MetricEntry(
                layer="backend",
                status="not_evaluated",
                value=None,
                declared=None,
                evidence="",
            ),
        },
        shortboards=(MetricShortboard(code="pending_nightly", metric="mutation_score", detail="M2"),),
        floor_ratio=None,
    )
    decision = evaluate_metrics_sufficiency(
        doc, load_policy_bytes(None, origin="packaged").evidence_sufficiency
    )
    assert decision.verdict == "pass"
    assert doc.floor_ratio is None


# --------------------------------------------------------------------------- #
# isolation: metrics never move execution / healing / archive
# --------------------------------------------------------------------------- #


def test_healing_loop_gate_does_not_read_metrics() -> None:
    gate = _schema().gates["healing-loop-gate"]
    paths = {entry.path for entry in gate.reads}
    assert METRICS_REL not in paths
    assert "inspect/metrics.json" not in paths


def test_archive_gate_blocks_on_metrics_collection_gap() -> None:
    """``archive-gate`` reads metrics only to enforce the ``reject`` block.

    It must not re-derive floors/cadence (that stays in
    ``evaluate_metrics_sufficiency``) — only ``collection_gaps`` participates.
    """
    gate = _schema().gates["archive-gate"]
    paths = {entry.path for entry in gate.reads}
    assert METRICS_REL in paths
    assert "archive.metrics_collection_gap" in gate.causes
    joined = gate.causes["archive.metrics_collection_gap"]
    assert "collection_gaps" in joined
    assert "floor" not in " ".join(gate.causes.values())


def test_healing_loop_reject_expression_ignores_metrics_shortboards() -> None:
    """A FAIL final_status is execution-only; metrics gaps must not appear in DSL."""
    gate = _schema().gates["healing-loop-gate"]
    joined = " ".join(rule.expr for rule in gate.rules)
    assert "metrics" not in joined
    assert "floor" not in joined
    assert "collection_gap" not in joined


def test_metrics_gate_writes_nothing_to_execution_manifest_contract() -> None:
    """The gate is builtin:gate — no write surface; materialize already pinned."""
    assert GATE_ID in _schema().gates
    # No execution-contract write for a gate id; isolation is structural.
    catalog = load_execution_contracts(Path.cwd())
    for target, contract in catalog.contracts.items():
        if "metrics" in target:
            assert "change:execution/execution-manifest.json" not in contract.writes
