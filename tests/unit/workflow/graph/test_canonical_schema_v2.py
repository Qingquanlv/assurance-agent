"""Canonical workflow-schema-v2 + packaged execution contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import assurance_agent.workflow.graph.runtime  # noqa: F401 — install Task 12 planner patches
from assurance_agent.workflow.graph.compiler import canonical_digest, compile_workflow, resolve_params
from assurance_agent.workflow.core.graph_events import NodeSkippedEvent
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourcePath,
    load_execution_contracts,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    ResolvedArtifact,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2

EXPECTED_GRAPHS = {
    "bootstrap",
    "workflow",
    "intake-workflow",
    "execute-workflow",
    "archive-workflow",
    "retro-workflow",
    "intake",
    "case-review-cycle",
    "assurance",
    "api-branch",
    "api-plan-cycle",
    "e2e-branch",
    "e2e-plan-cycle",
    "fuzz-branch",
    "fuzz-plan-review-cycle",
    "performance-branch",
    "performance-plan-review-cycle",
    "inspect-with-issues",
    "healing",
    # Issue review entrypoints (Task 12)
    "issue-review-workflow",
    "issue-analyze-workflow",
    "issue-reconcile-workflow",
    # Improvement review (retro/improvement separation Task 10)
    "improvement-review-workflow",
}

EXPECTED_CONTRACTS = {
    "skill:aa-explore",
    "skill:aa-case-design",
    "skill:aa-case-reviewer",
    "skill:aa-case-fixer",
    "skill:aa-fact-baseline",
    "skill:aa-api-plan",
    "skill:aa-api-plan-reviewer",
    "skill:aa-api-plan-fixer",
    "skill:aa-api-codegen",
    "skill:aa-e2e-plan",
    "skill:aa-e2e-plan-reviewer",
    "skill:aa-e2e-plan-fixer",
    "skill:aa-e2e-codegen",
    "skill:aa-fuzz-plan",
    "skill:aa-fuzz-plan-reviewer",
    "skill:aa-fuzz-codegen",
    "skill:aa-performance-plan",
    "skill:aa-performance-plan-reviewer",
    "skill:aa-performance-codegen",
    "skill:aa-inspect",
    "skill:aa-fix-proposal",
    "skill:aa-api-codegen-fixer",
    "skill:aa-e2e-codegen-fixer",
    "skill:aa-report-generator",
    "skill:aa-archive",
    "operation:no-op",
    "operation:skill-registry-check",
    "operation:run-tests",
    "operation:inspect",
    "operation:allocate-healing-attempt",
    "operation:record-healing-status",
    "operation:stop",
    "operation:retro-collect",
    "operation:reconcile-improvements",
    "skill:aa-retro",
    "builtin:join",
    "builtin:gate",
    "builtin:interrupt",
    # Issue lifecycle (Task 8-11)
    "skill:aa-issue-analyzer",
    "skill:aa-issue-triage-advisor",
    "operation:collect-observations",
    "operation:record-empty-issue-analysis",
    "operation:record-issue-analysis-failure",
    "operation:record-project-sync-pending",
    "operation:reconcile-issues",
    # Issue review (Task 12)
    "operation:load-problem-review-context",
    "operation:apply-problem-review",
    # Improvement review (retro/improvement separation Task 10)
    "operation:load-improvement-review-context",
    "operation:apply-improvement-review",
}

SCHEMA_REL = Path("assurance_agent/_resources/schemas/workflow-schema.yaml")
BRANCH_NODES = ("api", "e2e", "fuzz", "performance")


class _EmptyArtifacts:
    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        raise KeyError(logical_path)


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


def _load_compiled() -> tuple[CompiledWorkflow, ExecutionContractCatalog]:
    schema = load_workflow_v2(Path.cwd(), SCHEMA_REL)
    contracts = load_execution_contracts(Path.cwd())
    return compile_workflow(schema, contracts), contracts


def _context(tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "change",
        change_id="change-1",
    )


def _assurance_projection(
    compiled: CompiledWorkflow,
    params: dict[str, object],
    *,
    tasks: list[TaskProjection] | None = None,
    supersteps: int = 0,
) -> GraphProjection:
    return GraphProjection(
        invocation_id="inv-assurance",
        entrypoint="assurance",
        checkpoint_ns="inv-assurance",
        parent_invocation_id="inv-parent",
        parent_task_id="parent-task",
        structural_path="execute-workflow/assurance/assurance",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=params,
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=supersteps,
        tasks={task.task_id: task for task in tasks or []},
    )


def _task(task: ExecutableTask, status: str = "succeeded", **overrides: object) -> TaskProjection:
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "node_id": task.node_id,
        "status": status,
        "attempts_used": 1,
        "latest_attempt_id": f"{task.task_id}-a1",
    }
    payload.update(overrides)
    return TaskProjection(**payload)  # type: ignore[arg-type]


def _plan_assurance(
    compiled: CompiledWorkflow,
    tmp_path: Path,
    params: dict[str, object],
    *,
    tasks: list[TaskProjection] | None = None,
    supersteps: int = 0,
):
    return plan_superstep(
        compiled,
        _assurance_projection(compiled, params, tasks=tasks, supersteps=supersteps),
        _context(tmp_path),
        _EmptyArtifacts(),
    )


def _healing_projection(
    compiled: CompiledWorkflow,
    params: dict[str, object],
    *,
    tasks: list[TaskProjection] | None = None,
) -> GraphProjection:
    return GraphProjection(
        invocation_id="inv-healing",
        entrypoint="healing",
        checkpoint_ns="inv-healing",
        parent_invocation_id="inv-parent",
        parent_task_id="parent-task",
        structural_path="assurance/healing/healing",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=params,
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        tasks={task.task_id: task for task in tasks or []},
    )


def _plan_healing(
    compiled: CompiledWorkflow,
    tmp_path: Path,
    params: dict[str, object],
    *,
    tasks: list[TaskProjection] | None = None,
    artifacts: _EmptyArtifacts | _FakeArtifacts | None = None,
):
    return plan_superstep(
        compiled,
        _healing_projection(compiled, params, tasks=tasks),
        _context(tmp_path),
        artifacts or _EmptyArtifacts(),
    )


# ---------------------------------------------------------------------------
# Step 1: inventory


def test_canonical_v2_compiles_with_all_targets() -> None:
    schema = load_workflow_v2(Path.cwd(), SCHEMA_REL)
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_workflow(schema, contracts)
    assert set(compiled.graphs) == EXPECTED_GRAPHS
    assert set(compiled.entrypoints) == {
        "full", "intake", "execute", "case", "archive", "retro",
        # Issue review entrypoints (Task 12)
        "issue-review", "issue-analyze", "issue-reconcile",
        # Improvement review (retro/improvement separation Task 10)
        "improvement-review",
    }


def test_archive_entrypoint_and_subgraph() -> None:
    compiled, _ = _load_compiled()
    assert "archive" in compiled.entrypoints
    assert compiled.entrypoints["archive"].graph_id == "archive-workflow"
    assert compiled.entrypoints["archive"].param_overrides.get("auto_archive") is True
    parent = compiled.graphs["workflow"].nodes["archive"]
    assert parent.definition.uses == "graph:archive-workflow"
    graph = compiled.graphs["archive-workflow"]
    arch = graph.nodes["archive"]
    assert arch.definition.uses == "skill:aa-archive"


def test_archive_gate_blocks_before_evidence_is_copied() -> None:
    """The gate must precede the skill and route stop to STOP.

    An attached gate on ``archive`` evaluates only after the node succeeded, and
    the node declares the archive dir as a required output — so a stop verdict
    would be recorded while the evidence was already copied and committed.
    """
    compiled, _ = _load_compiled()
    graph = compiled.schema.graphs["archive-workflow"]
    assert graph.nodes["archive"].gate is None
    assert graph.nodes["precheck"].uses == "builtin:gate"
    assert graph.nodes["precheck"].with_ == {"gate": "archive-gate"}
    assert [(e.from_, e.to) for e in graph.edges] == [("START", "precheck"), ("archive", "END")]
    route = next(r for r in graph.routes if r.from_ == "precheck")
    assert route.select == "node('precheck').gate.verdict"
    assert route.cases == {"pass": "archive", "stop": "STOP"}
    assert route.default == "STOP"


def test_packaged_contracts_cover_exact_targets() -> None:
    contracts = load_execution_contracts(Path.cwd())
    assert set(contracts.contracts) == EXPECTED_CONTRACTS


def test_healing_fixers_carry_narrow_per_node_claims() -> None:
    """fix-api / fix-e2e must claim their own contract scope, not the whole-graph
    footprint — otherwise every healing node serializes against every other."""
    compiled, _ = _load_compiled()
    healing = compiled.graphs["healing"]
    fix_api = healing.nodes["fix-api"].resources
    fix_e2e = healing.nodes["fix-e2e"].resources
    footprint = healing.resource_footprint
    # Per-node claims are strictly narrower than the conservative graph union.
    assert fix_api != footprint
    assert fix_e2e != footprint
    assert ResourcePath.parse("repo:tests/api/**") in fix_api.writes
    assert ResourcePath.parse("repo:tests/e2e/**") in fix_e2e.writes
    assert ResourcePath.parse("repo:tests/e2e/**") not in fix_api.writes


# ---------------------------------------------------------------------------
# Step 3: assurance first two supersteps + active branches


@pytest.mark.parametrize(
    ("overrides", "step1", "step2_active"),
    [
        ({"run_mode": "full", "test_types": ["api", "e2e"]}, ["fact-baseline"], {"api", "e2e"}),
        ({"run_mode": "api-only", "test_types": ["api"]}, ["fact-baseline"], {"api"}),
        ({"run_mode": "e2e-only", "test_types": ["e2e"]}, ["fact-baseline"], {"e2e"}),
        (
            {"run_mode": "plan-only", "test_types": ["api", "e2e"]},
            ["fact-baseline"],
            {"api", "e2e"},
        ),
        (
            {"run_mode": "codegen-only", "test_types": ["api", "e2e"]},
            ["api", "e2e"],
            {"generation-join"},
        ),
        (
            {"run_mode": "review-plan", "test_types": ["api", "e2e"]},
            ["fact-baseline"],
            {"api", "e2e"},
        ),
    ],
)
def test_assurance_first_two_supersteps_and_active_branches(
    tmp_path: Path,
    overrides: dict[str, object],
    step1: list[str],
    step2_active: set[str],
) -> None:
    compiled, _ = _load_compiled()
    params = resolve_params(compiled.schema, overrides)

    first = _plan_assurance(compiled, tmp_path, params)
    assert [task.node_id for task in first.tasks] == step1

    if overrides["run_mode"] == "codegen-only":
        succeeded = [_task(task) for task in first.tasks]
        second = _plan_assurance(compiled, tmp_path, params, tasks=succeeded, supersteps=1)
        assert {task.node_id for task in second.tasks} == step2_active
        return

    fact = first.tasks[0]
    second = _plan_assurance(compiled, tmp_path, params, tasks=[_task(fact)], supersteps=1)
    active = {task.node_id for task in second.tasks}
    skipped = {
        e.node_id
        for e in second.strict_events
        if isinstance(e, NodeSkippedEvent) and e.node_id in BRANCH_NODES
    }
    assert active == step2_active
    assert active | skipped == set(BRANCH_NODES)


def test_assurance_overlapping_test_types_activate_all_branches(tmp_path: Path) -> None:
    compiled, _ = _load_compiled()
    params = resolve_params(
        compiled.schema,
        {"run_mode": "full", "test_types": ["api", "e2e", "fuzz", "performance"]},
    )
    first = _plan_assurance(compiled, tmp_path, params)
    fact = first.tasks[0]
    second = _plan_assurance(compiled, tmp_path, params, tasks=[_task(fact)], supersteps=1)
    assert {task.node_id for task in second.tasks} == set(BRANCH_NODES)


# ---------------------------------------------------------------------------
# Step 4: healing routes + planner outcomes


def _healing_routes(compiled: CompiledWorkflow) -> dict[str, dict[str, str]]:
    graph = compiled.schema.graphs["healing"]
    out: dict[str, dict[str, str]] = {}
    for route in graph.routes:
        out[route.from_] = dict(route.cases)
    return out


def test_healing_routes_cover_required_outcomes() -> None:
    compiled, _ = _load_compiled()
    routes = _healing_routes(compiled)
    assert routes["entry"] == {
        "enter": "proposal",
        "skip": "complete-not-needed",
        "stop": "complete-failed",
        "reject": "complete-failed",
    }
    assert routes["safety"]["pass"] == "rerun"
    assert routes["safety"]["needs_human_review"] == "safety-interrupt"
    assert routes["safety-interrupt"] == {
        "accept_risk": "rerun",
        "fix_and_proceed": "proposal",
        "stop": "complete-failed",
    }
    assert routes["decide"] == {
        "exit": "complete-resolved",
        "continue": "proposal",
        "stop": "complete-exhausted",
        "reject": "complete-failed",
    }
    # No resume path returns to the same unresolved interrupt.
    assert "safety-interrupt" not in routes["safety-interrupt"].values()

    allocate = compiled.schema.graphs["healing"].nodes["allocate"]
    assert allocate.budget is not None
    assert allocate.budget.consume == "healing_attempts"
    assert allocate.budget.exhausted_to == "complete-exhausted"
    consumers = [
        nid for nid, node in compiled.schema.graphs["healing"].nodes.items() if node.budget is not None
    ]
    assert consumers == ["allocate"]


@pytest.mark.parametrize(
    ("eligible", "expected_fixers"),
    [
        ([], set()),
        ([{"target": "api", "eligible": True}], {"fix-api"}),
        (
            [
                {"target": "api", "eligible": True},
                {"target": "e2e", "eligible": True},
            ],
            {"fix-api", "fix-e2e"},
        ),
    ],
)
def test_healing_fixer_activation_after_allocate(
    tmp_path: Path,
    eligible: list[dict[str, object]],
    expected_fixers: set[str],
) -> None:
    compiled, _ = _load_compiled()
    params = resolve_params(compiled.schema, {"run_mode": "full"})
    # Seed allocate as succeeded; planner re-evaluates fixer `when` against artifacts.
    # Without fix_proposal artifact symbols, any(...) is MISSING → skip. Simulate by
    # injecting succeeded allocate and using node.when against empty scope: both skip
    # when proposals missing (zero eligible). For non-empty, we assert route targets only.
    if not eligible:
        first = _plan_healing(compiled, tmp_path, params)
        assert [task.node_id for task in first.tasks] == ["entry"]
        entry = first.tasks[0]
        after_entry = _plan_healing(
            compiled,
            tmp_path,
            params,
            tasks=[
                _task(
                    entry,
                    gate_report={"gate_id": "healing-entry-gate", "verdict": "enter", "value": "enter"},
                )
            ],
        )
        assert [task.node_id for task in after_entry.tasks] == ["proposal"]
        return

    proposal_doc = {"proposals": eligible}
    artifacts = _FakeArtifacts({("tree-0", "change:healing/fix-proposal.json"): proposal_doc})

    entry_plan = _plan_healing(compiled, tmp_path, params, artifacts=artifacts)
    entry = entry_plan.tasks[0]
    proposal_plan = _plan_healing(
        compiled,
        tmp_path,
        params,
        artifacts=artifacts,
        tasks=[
            _task(
                entry,
                gate_report={"gate_id": "healing-entry-gate", "verdict": "enter", "value": "enter"},
            )
        ],
    )
    proposal = proposal_plan.tasks[0]
    eligible_plan = _plan_healing(
        compiled,
        tmp_path,
        params,
        artifacts=artifacts,
        tasks=[
            _task(
                entry,
                gate_report={"gate_id": "healing-entry-gate", "verdict": "enter", "value": "enter"},
            ),
            _task(proposal),
        ],
    )
    proposal_eligible = next(task for task in eligible_plan.tasks if task.node_id == "proposal-eligible")
    allocate_plan = _plan_healing(
        compiled,
        tmp_path,
        params,
        artifacts=artifacts,
        tasks=[
            _task(
                entry,
                gate_report={"gate_id": "healing-entry-gate", "verdict": "enter", "value": "enter"},
            ),
            _task(proposal),
            _task(proposal_eligible, value=True, gate_report={"expression": "...", "value": True}),
        ],
    )
    allocate = next(task for task in allocate_plan.tasks if task.node_id == "allocate")
    seeded = [
        _task(
            entry,
            gate_report={"gate_id": "healing-entry-gate", "verdict": "enter", "value": "enter"},
        ),
        _task(proposal),
        _task(proposal_eligible, value=True, gate_report={"expression": "...", "value": True}),
        _task(allocate),
    ]
    fixer_plan = _plan_healing(
        compiled,
        tmp_path,
        params,
        artifacts=artifacts,
        tasks=seeded,
    )
    activated = {task.node_id for task in fixer_plan.tasks}
    assert expected_fixers.issubset(activated)

    with pytest.raises(PlanError, match="all_active"):
        _plan_healing(compiled, tmp_path, params, tasks=seeded)

    healing = compiled.schema.graphs["healing"]
    assert healing.nodes["fix-api"].when is not None
    assert healing.nodes["fix-e2e"].when is not None
    if expected_fixers == {"fix-api", "fix-e2e"}:
        assert healing.nodes["fixer-join"].join is not None
        assert healing.nodes["fixer-join"].join.mode == "all_active"


def test_healing_completion_and_interrupt_terminals() -> None:
    compiled, _ = _load_compiled()
    healing = compiled.schema.graphs["healing"]
    for status_node in (
        "complete-not-needed",
        "complete-resolved",
        "complete-exhausted",
        "complete-failed",
    ):
        assert healing.nodes[status_node].uses == "operation:record-healing-status"
    interrupt = healing.nodes["safety-interrupt"].interrupt
    assert interrupt is not None
    assert set(interrupt.actions) == {"fix_and_proceed", "accept_risk", "stop"}


# ---------------------------------------------------------------------------
# Step 6: canonical safety properties


def test_every_cyclic_scc_has_budget_consumer_and_exhausted_exit() -> None:
    compiled, _ = _load_compiled()
    for graph_id, compiled_graph in compiled.graphs.items():
        schema_graph = compiled.schema.graphs[graph_id]
        for scc in compiled_graph.sccs:
            outs = {nid: _outgoing(schema_graph, nid) for nid in scc}
            cyclic = len(scc) > 1 or any(nid in outs[nid] for nid in scc)
            if not cyclic:
                continue
            consumers = [nid for nid in scc if schema_graph.nodes[nid].budget is not None]
            assert consumers, f"{graph_id} SCC {scc} lacks budget consumer"
            for nid in consumers:
                budget = schema_graph.nodes[nid].budget
                assert budget is not None
                assert budget.exhausted_to not in scc


def _outgoing(graph, nid: str) -> set[str]:
    targets: set[str] = set()
    for edge in graph.edges:
        if edge.from_ == nid and edge.to not in {"END", "STOP", "FAIL"}:
            targets.add(edge.to)
    for route in graph.routes:
        if route.from_ == nid:
            for target in route.cases.values():
                if target not in {"END", "STOP", "FAIL"}:
                    targets.add(target)
            if route.default is not None and route.default not in {"END", "STOP", "FAIL"}:
                targets.add(route.default)
    return targets


def test_gate_routes_exhaustive_or_fail_closed_and_interrupts_routed() -> None:
    compiled, _ = _load_compiled()
    for graph_id, graph in compiled.schema.graphs.items():
        for route in graph.routes:
            if route.select.startswith("resume."):
                continue
            assert route.default is not None or route.cases, f"{graph_id}/{route.from_}"
        for nid, node in graph.nodes.items():
            if node.interrupt is None:
                continue
            routed = {label for route in graph.routes if route.from_ == nid for label in route.cases}
            assert set(node.interrupt.actions) <= routed, f"{graph_id}/{nid}"


def test_attached_gate_verdicts_reach_control_flow() -> None:
    """A gate whose verdict no route reads cannot block anything.

    The gate still runs and still freezes a ``gate_report``, so the ledger shows a
    ``stop`` verdict while the node's write-set commits regardless — silent by
    construction. Every attached gate in the canonical schema must therefore have a
    route selecting its verdict.
    """
    compiled, _ = _load_compiled()
    unrouted: set[tuple[str, str]] = set()
    for graph_id, graph in compiled.schema.graphs.items():
        for nid, node in graph.nodes.items():
            if node.gate is None:
                continue
            # Either the literal verdict selector or a helper selector (e.g.
            # ``plan_review_route('review')``) that reads this node's result.
            if any(
                route.from_ == nid and (f"node('{nid}')" in route.select or f"('{nid}')" in route.select)
                for route in graph.routes
            ):
                continue
            unrouted.add((graph_id, nid))
    assert unrouted == set()


def test_codegen_is_reachable_only_through_a_gate_route() -> None:
    """Codegen must sit behind a routed gate, never on a plain edge.

    An edge into ``codegen`` cannot express a precondition: ``when`` has no access to
    ``gate()``, so a direct ``review-cycle -> codegen`` edge generates the suite no
    matter what the plan review said.
    """
    compiled, _ = _load_compiled()
    for graph_id in ("api-branch", "e2e-branch", "fuzz-branch", "performance-branch"):
        graph = compiled.schema.graphs[graph_id]
        assert not [e for e in graph.edges if e.to == "codegen"], graph_id
        gates_into_codegen = {
            route.from_ for route in graph.routes if "codegen" in set(route.cases.values()) | {route.default}
        }
        assert gates_into_codegen, graph_id
        for nid in gates_into_codegen:
            assert graph.nodes[nid].uses == "builtin:gate", f"{graph_id}/{nid}"


def test_all_node_targets_resolve_without_unknown_resources() -> None:
    compiled, contracts = _load_compiled()
    for graph_id, graph in compiled.schema.graphs.items():
        for nid, node in graph.nodes.items():
            prefix, _, _ = node.uses.partition(":")
            if prefix == "graph":
                assert node.uses.removeprefix("graph:") in compiled.graphs
            else:
                assert node.uses in contracts.contracts, f"{graph_id}/{nid}: {node.uses}"
        footprint = compiled.graphs[graph_id].resource_footprint
        assert "global:exclusive" not in footprint.exclusive, f"{graph_id} has unknown footprint"


def test_codegen_write_claims_are_disjoint_across_suites() -> None:
    contracts = load_execution_contracts(Path.cwd())
    # ``repo:tests/testdata/**`` holds shared, business-valid domain factories
    # (create-if-missing / reuse). It is deliberately shared across all suites and
    # its writes are serialized by the ``repo:test-infra`` exclusive lock, so it is
    # excluded from the per-suite disjointness invariant (which protects each
    # suite's private test dir from cross-suite clobbering).
    shared = {"repo:tests/testdata/**"}
    writes = {
        "api": set(contracts.contracts["skill:aa-api-codegen"].writes) - shared,
        "e2e": set(contracts.contracts["skill:aa-e2e-codegen"].writes) - shared,
        "fuzz": set(contracts.contracts["skill:aa-fuzz-codegen"].writes) - shared,
        "performance": set(contracts.contracts["skill:aa-performance-codegen"].writes) - shared,
    }
    assert writes["api"].isdisjoint(writes["e2e"])
    assert writes["api"].isdisjoint(writes["fuzz"])
    assert writes["api"].isdisjoint(writes["performance"])
    assert writes["e2e"].isdisjoint(writes["fuzz"])
    assert writes["e2e"].isdisjoint(writes["performance"])
    assert writes["fuzz"].isdisjoint(writes["performance"])
    # The shared factory claim is present in every codegen suite (all layers may
    # create-if-missing / reuse it under the serializing test-infra lock).
    for suite in ("aa-api-codegen", "aa-e2e-codegen", "aa-fuzz-codegen", "aa-performance-codegen"):
        assert "repo:tests/testdata/**" in contracts.contracts[f"skill:{suite}"].writes
    run_tests = contracts.contracts["operation:run-tests"]
    assert "repo:test-runtime" in run_tests.exclusive
    assert any(r.startswith("repo:tests/") for r in run_tests.reads)


def _full_graph_closure(schema) -> list:
    """Graphs reachable from the full workflow entrypoint via ``graph:`` edges."""
    start = schema.entrypoints["full"].graph
    seen: set[str] = set()
    stack = [start]
    ordered: list = []
    while stack:
        gid = stack.pop()
        if gid in seen or gid not in schema.graphs:
            continue
        seen.add(gid)
        graph = schema.graphs[gid]
        ordered.append(graph)
        for node in graph.nodes.values():
            prefix, _, target = node.uses.partition(":")
            if prefix == "graph" and target in schema.graphs and target not in seen:
                stack.append(target)
    return ordered


def test_retro_graph_is_independent_and_closed() -> None:
    compiled, _ = _load_compiled()
    schema = compiled.schema
    assert compiled.entrypoints["retro"].graph_id == "retro-workflow"
    retro = schema.graphs["retro-workflow"]
    assert tuple(retro.nodes) == (
        "collect-retro-evidence",
        "propose-improvements",
        "reconcile-improvements",
    )
    assert retro.nodes["collect-retro-evidence"].uses == "operation:retro-collect"
    assert retro.nodes["propose-improvements"].uses == "skill:aa-retro"
    assert retro.nodes["reconcile-improvements"].uses == "operation:reconcile-improvements"
    full_targets = {
        node.uses for graph in _full_graph_closure(schema) for node in graph.nodes.values()
    }
    assert not any("retro" in target or "improvement" in target for target in full_targets)


def test_retro_entrypoint_topology() -> None:
    compiled, _ = _load_compiled()
    assert compiled.entrypoints["retro"].graph_id == "retro-workflow"
    g = compiled.graphs["retro-workflow"]
    assert set(g.nodes) == {
        "collect-retro-evidence",
        "propose-improvements",
        "reconcile-improvements",
    }
    assert g.nodes["collect-retro-evidence"].definition.uses == "operation:retro-collect"
    assert g.nodes["propose-improvements"].definition.uses == "skill:aa-retro"
    assert g.nodes["reconcile-improvements"].definition.uses == "operation:reconcile-improvements"
    schema_graph = compiled.schema.graphs["retro-workflow"]
    collect_edges = [e for e in schema_graph.edges if e.from_ == "collect-retro-evidence"]
    assert any(e.to == "END" and e.when for e in collect_edges)
    assert any(e.to == "propose-improvements" and e.when for e in collect_edges)
    assert any(
        e.when and "retro_dry_run" in e.when and "signal_count" in e.when for e in collect_edges
    )


# ---------------------------------------------------------------------------
# Task 11: inspect-with-issues topology


def test_assurance_uses_inspect_with_issues_subgraph() -> None:
    """execution -> inspect-with-issues (subgraph) -> healing -> report."""
    compiled, _ = _load_compiled()
    assurance = compiled.schema.graphs["assurance"]
    # The assurance graph calls inspect-with-issues as a subgraph, not directly.
    assert assurance.nodes["inspect-with-issues"].uses == "graph:inspect-with-issues"
    assert "inspect" not in assurance.nodes  # old direct inspect node is gone
    edge_pairs = {(e.from_, e.to) for e in assurance.edges}
    assert ("execution", "inspect-with-issues") in edge_pairs
    assert ("inspect-with-issues", "healing") in edge_pairs


def test_healing_uses_inspect_with_issues_subgraph() -> None:
    """healing.rerun -> inspect-with-issues (subgraph) -> decide."""
    compiled, _ = _load_compiled()
    healing = compiled.schema.graphs["healing"]
    assert healing.nodes["inspect-with-issues"].uses == "graph:inspect-with-issues"
    assert "reinspect" not in healing.nodes  # old direct reinspect node is gone
    edge_pairs = {(e.from_, e.to) for e in healing.edges}
    assert ("rerun", "inspect-with-issues") in edge_pairs
    assert ("inspect-with-issues", "decide") in edge_pairs


def test_run_tests_false_skips_execution_and_issue_subgraph(tmp_path: Path) -> None:
    """run_tests=false must send generation-join -> END, bypassing both
    execution and the inspect-with-issues Issue subgraph."""
    compiled, _ = _load_compiled()
    assurance = compiled.schema.graphs["assurance"]
    skip_edge = next(
        e for e in assurance.edges if e.from_ == "generation-join" and e.to == "END"
    )
    assert skip_edge.when is not None
    assert "run_tests" in skip_edge.when
    assert "false" in skip_edge.when.lower() or "== false" in skip_edge.when


def test_inspect_with_issues_subgraph_structure() -> None:
    """inspect-with-issues must have the required nodes in correct roles."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    assert g.nodes["inspect"].uses == "operation:inspect"
    assert g.nodes["collect-observations"].uses == "operation:collect-observations"
    assert g.nodes["analyze-issues"].uses == "skill:aa-issue-analyzer"
    assert g.nodes["record-empty-analysis"].uses == "operation:record-empty-issue-analysis"
    assert g.nodes["reconcile-issues"].uses == "operation:reconcile-issues"
    assert g.nodes["record-analysis-failure"].uses == "operation:record-issue-analysis-failure"
    assert g.nodes["record-project-sync-pending"].uses == "operation:record-project-sync-pending"
    assert g.nodes["inspect-complete"].uses == "operation:no-op"


def test_inspect_with_issues_operation_inspect_is_first() -> None:
    """operation:inspect must be the first node after START, preserving
    failure-analysis / quality-gate authority."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    start_targets = {e.to for e in g.edges if e.from_ == "START"}
    assert start_targets == {"inspect"}
    after_inspect = {e.to for e in g.edges if e.from_ == "inspect"}
    assert after_inspect == {"collect-observations"}


def test_inspect_with_issues_collect_branches_on_abnormal_count() -> None:
    """collect-observations fans out to analyze-issues when abnormal_count > 0
    and to record-empty-analysis when abnormal_count == 0."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    collect_edges = [e for e in g.edges if e.from_ == "collect-observations"]
    targets = {e.to for e in collect_edges}
    assert targets == {"analyze-issues", "record-empty-analysis"}
    for edge in collect_edges:
        assert edge.when is not None, f"conditional edge to {edge.to} missing when clause"
        assert "abnormal_count" in edge.when


def test_inspect_with_issues_analyzer_recovery() -> None:
    """analyze-issues carries typed recovery to record-analysis-failure,
    continuing to inspect-complete on recovery success."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    analyzer_node = g.nodes["analyze-issues"]
    assert analyzer_node.recover is not None
    recover = analyzer_node.recover
    assert set(recover.errors) == {"timeout", "transport", "rate_limit", "invalid_output"}
    assert recover.via == "record-analysis-failure"
    assert recover.continue_to == "inspect-complete"
    # Recovery nodes must not have ordinary incoming/outgoing edges.
    assert not any(e.to == "record-analysis-failure" for e in g.edges)
    assert not any(e.from_ == "record-analysis-failure" for e in g.edges)


def test_inspect_with_issues_reconcile_recovery() -> None:
    """reconcile-issues carries typed recovery to record-project-sync-pending,
    continuing to inspect-complete on recovery success."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    reconcile_node = g.nodes["reconcile-issues"]
    assert reconcile_node.recover is not None
    recover = reconcile_node.recover
    assert set(recover.errors) == {"conflict", "transport"}
    assert recover.via == "record-project-sync-pending"
    assert recover.continue_to == "inspect-complete"
    # Recovery nodes must not have ordinary incoming/outgoing edges.
    assert not any(e.to == "record-project-sync-pending" for e in g.edges)
    assert not any(e.from_ == "record-project-sync-pending" for e in g.edges)


def test_inspect_with_issues_both_analysis_paths_reach_reconcile() -> None:
    """Both the normal analysis path and the empty-analysis path must reach
    reconcile-issues before inspect-complete."""
    compiled, _ = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    edge_pairs = {(e.from_, e.to) for e in g.edges}
    # analyze-issues success -> reconcile-issues
    assert ("analyze-issues", "reconcile-issues") in edge_pairs
    # record-empty-analysis -> reconcile-issues
    assert ("record-empty-analysis", "reconcile-issues") in edge_pairs
    # reconcile-issues -> inspect-complete
    assert ("reconcile-issues", "inspect-complete") in edge_pairs
    # inspect-complete -> END
    assert ("inspect-complete", "END") in edge_pairs


def test_inspect_with_issues_retry_policies_declared() -> None:
    """llm-default and project-sync retry policies must exist in the schema."""
    schema = load_workflow_v2(Path.cwd(), SCHEMA_REL)
    assert "llm-default" in schema.policies.retry
    assert "project-sync" in schema.policies.retry
    # analyze-issues uses llm-default
    iwi = schema.graphs["inspect-with-issues"]
    assert iwi.nodes["analyze-issues"].retry == "llm-default"
    # reconcile-issues uses project-sync
    assert iwi.nodes["reconcile-issues"].retry == "project-sync"


def test_inspect_with_issues_recovery_kinds_match_contracts(
    tmp_path: Path,
) -> None:
    """Recovery error kinds must be a subset of the contract's retryable_errors."""
    compiled, contracts = _load_compiled()
    g = compiled.schema.graphs["inspect-with-issues"]
    for nid, node in g.nodes.items():
        if node.recover is None:
            continue
        contract_target = node.uses
        if contract_target.startswith("graph:"):
            continue
        contract = contracts.contracts[contract_target]
        unsupported = set(node.recover.errors) - set(contract.retryable_errors)
        assert not unsupported, (
            f"inspect-with-issues/{nid} recovery errors {unsupported} "
            f"not in {contract_target}.retryable_errors"
        )


def test_schema_and_contract_digests_stable_across_two_loads() -> None:
    first, contracts_a = _load_compiled()
    second, contracts_b = _load_compiled()
    assert first.digest == second.digest
    assert first.contract_digests == second.contract_digests
    assert {
        target: canonical_digest(contracts_a.contracts[target]) for target in sorted(EXPECTED_CONTRACTS)
    } == {target: canonical_digest(contracts_b.contracts[target]) for target in sorted(EXPECTED_CONTRACTS)}
    # Wheel-resource load path (no project-local override).
    from assurance_agent import resources

    packaged = compile_workflow(
        load_workflow_v2(Path.cwd(), SCHEMA_REL),
        load_execution_contracts(Path("/tmp/aa-no-local-contracts-dir")),
    )
    assert packaged.digest == first.digest
    assert packaged.contract_digests == first.contract_digests
    assert resources.read_text("schemas", "execution-contracts.yaml")
