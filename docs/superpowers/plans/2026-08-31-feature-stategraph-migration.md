# Feature StateGraph Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all 50 Capability-owned YAML graphs with six authenticated Python `StateGraph` bundles, including the Execution tracer, typed business state, 12 Feature human interrupts, four Generation/one Intake `join:any` rewrites, Generation fanout, and repaired Improvement Retro/evaluate contracts.

**Architecture:** Each Capability owns its `graphs/` package and exposes only a small frozen bundle. Factories receive an owner-scoped `CapabilityBuildContext`, bind semantic Attempt IDs through `context.attempt`, use ordinary pure functions for deterministic state transforms, and compile subgraphs with `checkpointer=None`. Checkpointed state is bounded JSON-compatible control data. Reducers are explicit, deterministic, associative, commutative where parallel ordering is undefined, and idempotent under replay. Product sees only bundle exports, never Feature-internal projections or mutable builders.

**Tech Stack:** Python 3.11, LangGraph `1.2.11`, Pydantic v2 at graph boundaries, typed `StateGraph`, `Send`, `Command`, `interrupt`, pytest, scripted/real Attempt Kernel harnesses.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, sections 7–9, 11, 16–19, 22, migration Phases 3–4, and Feature factory tests.

---

## Global Constraints

- Foundation and Semantic Attempt Kernel exit gates must be green. Use their frozen `CapabilityBuildContext`, `AttemptNodeFactory`, runtime context, checkpointer, receipt reducer, and contract catalog; do not fork equivalent APIs inside a Feature.
- Execute in the clean migration worktree. Existing uncommitted Generation/Intake/OpenCode/YAML changes in the original worktree are user-owned and must not be staged or copied implicitly.
- Keep all six YAML modules, 99 aliases, and legacy graph tests during this plan. Python graphs run beside the old Runtime for characterization; deletion belongs to Product cutover.
- A Feature never imports another Feature's `graphs`, operations, handlers, validators, effects, resources, or plugin. Existing downward imports of public domain contracts remain subject to `.importlinter`. Product alone composes bundles.
- Graph packages do not import concrete runtime adapters, Product, registered effect handlers, or effectful operation implementations. They select typed inputs, call `context.attempt`, publish results, and run pure routes/state transforms.
- All Feature subgraphs use `context.compile_subgraph(builder)`, which always compiles with `checkpointer=None`; a Feature cannot access the root saver.
- Every Feature state `TypedDict` inherits framework `CheckpointBridgeState`. Graph business selectors and public adapters omit its reserved `assurance_checkpoint_markers` key; the Attempt factory alone writes the bounded replacement batch.
- Store only JSON-compatible data in checkpoint state. Pydantic models validate node boundaries and decisions, then publish `model_dump(mode="json")`. Do not checkpoint Pydantic class instances, secrets, service ports, workspaces, raw sessions, or artifact bytes.
- Preserve business budgets explicitly. `recursion_limit` is a separate Application circuit breaker, not the loop counter.
- Every `context.attempt` call supplies a required stable activation selector. One-shot nodes use an owner-local fixed activation; ordinary loops use their business round; join-driven nodes use `current_trigger.arrival_id`, so same-epoch late arrivals are distinct and replay is idempotent.
- The seven looped `join:any` sites retain every distinct arrival. Flow-local inbox reducers de-duplicate identical arrival IDs and merge order-independently; a dispatch cursor selects one stable unconsumed trigger at a time. A second/late arrival for the same business epoch is not discarded and not treated as an aggregate map—it may re-activate the downstream path once, matching legacy residual-token behavior.
- The two nonloop Product `join:any` sites are handled in the Product plan. This plan handles Generation #1–4 and Intake #5.
- Of three legacy `min_matches` sites, only Generation remains a target `Send` fanout. Intake's two phase-internal sites disappear inside `CompositeAttemptExecutor`; tests retain their two-consumer dataflow without phase nodes.
- Four legacy direct handler IDs are proven pure because their implementations discard `TaskContext` and only validate/transform input: Generation complete, Generation review-round advance, Intake review-round advance, and Healing repair-round advance. Move their models/functions into contract/graph modules and call them as ordinary nodes. The eight Improvement direct contracts remain Kernel Attempts.

## Target bundles and source inventory

```python
@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    case: CompiledStateGraph

@dataclass(frozen=True, slots=True)
class GenerationGraphs:
    generate: CompiledStateGraph

@dataclass(frozen=True, slots=True)
class ExecutionGraphs:
    execute: CompiledStateGraph
    rerun: CompiledStateGraph

@dataclass(frozen=True, slots=True)
class QualityGraphs:
    assess: CompiledStateGraph
    issue_review: CompiledStateGraph
    issue_analyze: CompiledStateGraph
    issue_reconcile: CompiledStateGraph
    report: CompiledStateGraph

@dataclass(frozen=True, slots=True)
class HealingGraphs:
    repair_failure: CompiledStateGraph
    repair_coverage: CompiledStateGraph

@dataclass(frozen=True, slots=True)
class ImprovementGraphs:
    archive: CompiledStateGraph
    retro: CompiledStateGraph
    review: CompiledStateGraph
    evaluate: CompiledStateGraph
    export: CompiledStateGraph
    apply: CompiledStateGraph
    rollback: CompiledStateGraph
```

The 50 legacy Feature graphs characterize source behavior: Intake 6, Generation 19, Execution 2, Quality 9, Healing 2, Improvement 12. Target private graph count may shrink because Agent phases and phase-internal joins collapse; bundle/public behavior is the acceptance surface.

### Task 1: Build the Feature graph test harness and freeze factory boundaries

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/testing/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/testing/graph_harness.py`
- Create: `packages/framework/graph-engine/graph_engine/testing/recording_build_context.py`
- Create: `packages/framework/graph-engine/graph_engine/stategraph/routing.py`
- Create: `packages/framework/graph-engine/tests/testing/test_graph_harness.py`
- Create: `packages/framework/graph-engine/tests/stategraph/test_routing.py`
- Create: `tests/architecture/test_python_graph_composition.py`
- Create: `tests/architecture/exclusive_route_inventory.py`
- Create: `tests/architecture/test_exclusive_route_migration.py`
- Modify: `.importlinter`

**Interfaces:** `ScriptedAttempt`, `RecordingCapabilityBuildContext`, `GraphHarness`; `select_exclusive_route(named_matches, otherwise=...)` with typed `AmbiguousRouteMatch`; a test-owned 50-site migration inventory; semantic call/route/interrupt/receipt/terminal trace; no private LangGraph task IDs.

- [ ] **Step 1: Write the harness contract tests.**

```python
async def test_harness_records_semantic_attempt_not_internal_task_identity() -> None:
    result = await harness.run(
        graph,
        input={"change_id": "chg-1"},
        script={"execution.run": [committed(output, receipt)]},
    )
    assert result.semantic_calls == (
        SemanticAttemptCall("execution.run", "assurance.execution.agent.run.v1", input_digest),
    )
    assert "task_id" not in result.model_dump_json()


def test_recording_context_rejects_foreign_contract_and_root_saver() -> None:
    with pytest.raises(ContractOwnershipError):
        intake_context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign-probe",
            activation=BusinessActivation.one_shot(),
            select=select_probe,
            publish=publish_probe,
        )
    assert not hasattr(intake_context, "checkpointer")
```

- [ ] **Step 2: Add architecture failures before Feature packages exist.**

Scan every `assurance_*/graphs/*.py` AST and reject imports of foreign `.graphs`, any Feature operations/validators/effects/resources/plugin, concrete adapters, or Product. Assert fixed factory modules are the only new Capability public graph surface.

While YAML remains, collect the exact 50 `(graph_id, node_id, node_kind, otherwise_target)` rows whose routing mode is `exclusive` into `EXCLUSIVE_ROUTE_INVENTORY`: 13 task, 20 subgraph, 11 interrupt and 6 gate nodes; 42 are Feature-owned and 8 Product-owned. Each row also names its target Python route test. `test_exclusive_route_migration.py` first proves set equality with the legacy compiler inventory, then requires each migrated row's parameterized test to exercise zero and two simultaneous named matches. Implement `select_exclusive_route()` as a small pure helper that evaluates the complete named-match mapping: exactly one returns its target, zero returns the explicit nonempty `otherwise`, and two or more raise `AmbiguousRouteMatch`. Route functions may not encode legacy exclusivity as priority `if/elif`.

- [ ] **Step 3: Run the intended RED.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/testing/test_graph_harness.py \
  packages/framework/graph-engine/tests/stategraph/test_routing.py \
  tests/architecture/test_python_graph_composition.py \
  tests/architecture/test_exclusive_route_migration.py
```

Expected: missing testing helpers; architecture test initially has no Feature factory packages to validate.

- [ ] **Step 4: Implement the scripted owner context and real compiled-graph harness.**

Use the Foundation anchored memory test backend for interrupt/persistence cases. Script only `AttemptResolution` values; run actual StateGraphs/routes/reducers. Record `select` values, semantic node/contract IDs, published update, interrupt envelope and terminal. Provide no fake method that marks every route successful.

- [ ] **Step 5: Keep `.workflow` and add `.graphs` prohibitions.**

During coexistence `.importlinter` forbids both suffixes across Features. Add architecture rules; do not remove legacy rules until final deletion.

- [ ] **Step 6: Verify and commit.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/testing/test_graph_harness.py \
  tests/architecture/test_python_graph_composition.py
uv run lint-imports
git add \
  .importlinter \
  packages/framework/graph-engine/graph_engine/testing/__init__.py \
  packages/framework/graph-engine/graph_engine/testing/graph_harness.py \
  packages/framework/graph-engine/graph_engine/testing/recording_build_context.py \
  packages/framework/graph-engine/graph_engine/stategraph/routing.py \
  packages/framework/graph-engine/tests/testing/test_graph_harness.py \
  packages/framework/graph-engine/tests/stategraph/test_routing.py \
  tests/architecture/exclusive_route_inventory.py \
  tests/architecture/test_exclusive_route_migration.py \
  tests/architecture/test_python_graph_composition.py
git commit -m "test: add semantic StateGraph harness"
```

### Task 2: Migrate Execution as the first real tracer

**Files:**

- Create: `packages/capabilities/assurance-execution/assurance_execution/graphs/__init__.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/graphs/state.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/graphs/nodes.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/graphs/factory.py`
- Create: `packages/capabilities/assurance-execution/tests/test_graph_factory.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/__init__.py`

**Interfaces:** `build_execution_graphs(context) -> ExecutionGraphs`; two semantic Agent nodes for IDs `assurance.execution.agent.execute.v1` and `assurance.execution.agent.run.v1`.

- [ ] **Step 1: Write the missing-factory RED.**

```python
def test_execution_factory_exports_execute_and_rerun(recording_context) -> None:
    bundle = build_execution_graphs(recording_context)
    assert bundle_fields(bundle) == ("execute", "rerun")
    assert recording_context.bound_contract_ids == (
        "assurance.execution.agent.execute.v1",
        "assurance.execution.agent.run.v1",
    )
    assert recording_context.compiled_subgraph_checkpointers == (None, None)
```

Add tests `test_execute_and_rerun_publish_typed_public_output` and `test_execution_graph_replays_committed_attempt_without_duplicate_dispatch`.

- [ ] **Step 2: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-execution/tests/test_graph_factory.py
```

Expected: `ModuleNotFoundError: assurance_execution.graphs`.

- [ ] **Step 3: Implement minimal direct graphs.**

Define JSON-compatible `ExecutionState`, Feature-local `select_execute/select_rerun`, `activation_execute=BusinessActivation.one_shot()`, parent-supplied typed `activation_rerun`, and `publish_execution`. Product passes the rerun business round/current failure-trigger arrival ID through the public child input; two reruns in one Invocation must not share a fixed activation even when their validated task inputs happen to match. Bind each graph through `context.attempt(..., semantic_node_id=..., activation=...)`; route committed/failure updates to explicit terminals. Do not create prepare/execute/finalize nodes or import the old YAML/GraphDef.

- [ ] **Step 4: Run through scripted then real Kernel.**

First use `GraphHarness` with committed/rejected/pending scripts. Then use a temporary workspace plus real Kernel/fake authenticated executor and assert one workspace promotion/receipt. Crash after promotion and replay the node; executor/promotion call count stays one.

- [ ] **Step 5: Verify and commit.**

```bash
uv run pytest -q packages/capabilities/assurance-execution/tests/test_graph_factory.py
uv run pytest -q packages/capabilities/assurance-execution/tests/test_contracts.py
uv run lint-imports
git add \
  packages/capabilities/assurance-execution/assurance_execution/__init__.py \
  packages/capabilities/assurance-execution/assurance_execution/graphs/__init__.py \
  packages/capabilities/assurance-execution/assurance_execution/graphs/state.py \
  packages/capabilities/assurance-execution/assurance_execution/graphs/nodes.py \
  packages/capabilities/assurance-execution/assurance_execution/graphs/factory.py \
  packages/capabilities/assurance-execution/tests/test_graph_factory.py
git commit -m "feat: migrate Execution to Python StateGraphs"
```

### Task 3: Migrate Intake, including the three-predecessor activation inbox

**Files:**

- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/__init__.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/state.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/nodes.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/routes.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/prepare.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/case.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/graphs/factory.py`
- Create: `packages/capabilities/assurance-intake/tests/test_graph_factory.py`
- Create: `packages/capabilities/assurance-intake/tests/test_graph_routes.py`
- Create: `packages/capabilities/assurance-intake/tests/test_graph_join_any.py`
- Create: `packages/capabilities/assurance-intake/tests/test_graph_interrupts.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/operations/workflow_state.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_workflow_state.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_workflow_module.py`

**Interfaces:** `build_intake_graphs(context) -> IntakeGraphs`; four contract IDs at five occurrences; `CaseReviewArrival/CaseReviewInbox`; typed review decision; pure round advance.

- [ ] **Step 1: Freeze the two legacy phase-fanout facts while YAML remains.**

Add passing characterization tests that `case-design/prepare` feeds both execute and finalize-inputs and repair-prepare feeds both repair-execute and repair-finalize-inputs, with `min_matches=2`. These tests prove the data obligation; they do not prescribe target nodes.

- [ ] **Step 2: Write factory and composite-Agent tests.**

Assert exact exports `prepare/case`, four Agent IDs, and five semantic occurrences because case-design has primary and repair node IDs. Assert target graphs contain no phase nodes. Primary and repair cases must prove the prepared value and validated Agent result both reach finalize through the one composite Attempt.

- [ ] **Step 3: Write all `entry/advance-join` semantics before routes.**

Use incoming predecessors `review-round-advance`, `review-round-advance-retry`, and `review-round-advance-rework-retry`. Tests cover:

```text
first arrival becomes exact current trigger
late second arrival in the same business epoch is retained and dispatched once
replay of the same arrival ID is de-duplicated
two reducer merge orders produce identical inbox state
repeated business epochs preserve exact rounds_used/rounds_budget input
dispatch cursor never reclaims a consumed arrival
downstream case-design-retry reads current_trigger.value only
```

`CaseReviewArrival` contains business epoch, predecessor enum, stable source activation/sequence, value, and canonical arrival ID. `CaseReviewInbox` contains sorted arrivals, dispatched IDs, and current trigger. Do not expose a generic token queue from `graph-engine`.

- [ ] **Step 4: Write human action and business-route tests.**

Both interrupt sites accept only `approve | reject | request_rework`. The pure interrupt node validates after restart and performs no pre-interrupt mutation. Test pass, automatic fix, request rework, reject, and budget exhaustion; each round advances exactly once with the pure typed function moved from `operations/workflow_state.py`.

- [ ] **Step 5: Run RED, then implement Feature-local state/routes/builders.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_graph_factory.py \
  packages/capabilities/assurance-intake/tests/test_graph_routes.py \
  packages/capabilities/assurance-intake/tests/test_graph_join_any.py \
  packages/capabilities/assurance-intake/tests/test_graph_interrupts.py
```

Expected initially: `ModuleNotFoundError: assurance_intake.graphs`; after base graph appears, late-arrival and composite dataflow tests remain red until implemented.

- [ ] **Step 6: Run legacy/new suites and commit.**

```bash
uv run pytest -q packages/capabilities/assurance-intake/tests
uv run lint-imports
git add \
  packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py \
  packages/capabilities/assurance-intake/assurance_intake/operations/workflow_state.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/__init__.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/state.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/nodes.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/routes.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/prepare.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/case.py \
  packages/capabilities/assurance-intake/assurance_intake/graphs/factory.py \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-intake/tests/test_workflow_state.py \
  packages/capabilities/assurance-intake/tests/test_graph_factory.py \
  packages/capabilities/assurance-intake/tests/test_graph_routes.py \
  packages/capabilities/assurance-intake/tests/test_graph_join_any.py \
  packages/capabilities/assurance-intake/tests/test_graph_interrupts.py
git commit -m "feat: migrate Intake to typed StateGraphs"
```

### Task 4: Migrate Generation fanout and four plan-review loops

**Files:**

- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/__init__.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/state.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/nodes.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/routes.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/api.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/e2e.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/fuzz.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/performance.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/graphs/factory.py`
- Create: `packages/capabilities/assurance-generation/tests/test_graph_factory.py`
- Create: `packages/capabilities/assurance-generation/tests/test_graph_routes.py`
- Create: `packages/capabilities/assurance-generation/tests/test_graph_join_any.py`
- Create: `packages/capabilities/assurance-generation/tests/test_graph_interrupts.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/workflow_state.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_state.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`

**Interfaces:** `build_generation_graphs(context) -> GenerationGraphs`; 14 Agent IDs; fixed four-family `Send`; commutative family-result reducer; four flow-local plan-round inboxes; pure completion/round advance.

- [ ] **Step 1: Freeze the legacy four-match behavior.**

Add a passing YAML characterization that root fanout has exactly four outgoing family selectors and `min_matches=4`, with no partial dispatch on insufficient selection.

- [ ] **Step 2: Write the target route tests.**

```python
def test_generation_route_emits_exactly_four_sends(valid_input) -> None:
    sends = route_families(valid_input)
    assert tuple(send.node for send in sends) == ("api", "e2e", "fuzz", "performance")


def test_generation_route_fails_before_producing_any_send(input_with_missing_lane) -> None:
    with pytest.raises(InsufficientRouteMatches):
        route_families(input_with_missing_lane)
```

Selected families produce selected/skip typed lane input, so the fixed structural barrier still receives four results. Validate nonempty known unique family selection before creating any `Send`.

- [ ] **Step 3: Test the family reducer and former `join:all`.**

The reducer de-duplicates by family/receipt ID and is associative, commutative and idempotent. Exercise all result arrival permutations. A four-family superstep must not write concurrent scalar keys; completion runs only after exactly one result for each family. The two Intake phase `join:all` sites are not recreated here or elsewhere.

- [ ] **Step 4: Parameterize all four `plan-round-join` behaviors.**

For api/e2e/fuzz/performance, incoming primary and retry round-advance arrivals enter a family-specific inbox. Test first arrival, same-epoch late arrival, replay dedup, merge-order independence, repeated epochs, dispatch cursor, and exact current-trigger `rounds_used/rounds_budget`. Test API/E2E codegen-fix loops and Fuzz/Performance no-fix topology separately.

- [ ] **Step 5: Test eight pure human interrupt nodes.**

Each family has primary/retry plan review interrupts and accepts only `approve | reject | request_rework`. Test action validation after restart, unchanged interrupt ID/ordinal, approval, rejection, rework, automatic fix, and budget exhaustion.

- [ ] **Step 6: Test exact contract and pure-node inventory.**

Factory binds exactly 14 Agent contract IDs. It binds no Task contract for `generation.complete` or `generation.review-round.advance`; instead it calls pure typed functions whose characterization matches existing handlers for valid/invalid inputs. Target graph has no prepare/execute/finalize phase nodes.

- [ ] **Step 7: Run RED, implement, and commit.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-generation/tests/test_graph_factory.py \
  packages/capabilities/assurance-generation/tests/test_graph_routes.py \
  packages/capabilities/assurance-generation/tests/test_graph_join_any.py \
  packages/capabilities/assurance-generation/tests/test_graph_interrupts.py
```

Expected initially: `ModuleNotFoundError: assurance_generation.graphs`; route and loop tests guide subsequent red/green cycles.

After all Generation tests and `uv run lint-imports` pass, stage exactly:

```bash
git add \
  packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py \
  packages/capabilities/assurance-generation/assurance_generation/operations/workflow_state.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/__init__.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/state.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/nodes.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/routes.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/api.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/e2e.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/fuzz.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/performance.py \
  packages/capabilities/assurance-generation/assurance_generation/graphs/factory.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_state.py \
  packages/capabilities/assurance-generation/tests/test_graph_factory.py \
  packages/capabilities/assurance-generation/tests/test_graph_routes.py \
  packages/capabilities/assurance-generation/tests/test_graph_join_any.py \
  packages/capabilities/assurance-generation/tests/test_graph_interrupts.py
git commit -m "feat: migrate Generation to typed StateGraphs"
```

### Task 5: Migrate the five Quality exports

**Files:**

- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/__init__.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/state.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/nodes.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/routes.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/assessment.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/issues.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/report.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/graphs/factory.py`
- Create: `packages/capabilities/assurance-quality/tests/test_graph_factory.py`
- Create: `packages/capabilities/assurance-quality/tests/test_graph_routes.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py`

**Interfaces:** five graph exports, five Agent contracts, typed coverage/failure/report outcomes, no Healing graph import.

- [ ] **Step 1: Write exact bundle/contract tests.**

Assert fields `assess`, `issue_review`, `issue_analyze`, `issue_reconcile`, `report`; exact five Agent IDs; subgraph checkpointer `None`; no private node table returned.

- [ ] **Step 2: Write route/projection behavior.**

Test assess publishes `coverage_state`, rounds and evidence; its public input requires a typed activation so Product distinguishes initial quality from every recheck. Issue review/analyze/reconcile remain independently callable exports; report receives change/coverage/execution/report references; unknown coverage/failure classification fails closed rather than selecting a first branch.

- [ ] **Step 3: Test dependency direction.**

Quality graphs may consume Healing public domain contracts only where current dependency rules allow, but cannot import `assurance_healing.graphs` or implementation. Healing selection stays Product-owned.

- [ ] **Step 4: Run RED, implement, verify, commit.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-quality/tests/test_graph_factory.py \
  packages/capabilities/assurance-quality/tests/test_graph_routes.py
uv run pytest -q packages/capabilities/assurance-quality/tests
uv run lint-imports
```

Expected initially: `ModuleNotFoundError: assurance_quality.graphs`. Stage exactly:

```bash
git add \
  packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/__init__.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/state.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/nodes.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/routes.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/assessment.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/issues.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/report.py \
  packages/capabilities/assurance-quality/assurance_quality/graphs/factory.py \
  packages/capabilities/assurance-quality/tests/test_graph_factory.py \
  packages/capabilities/assurance-quality/tests/test_graph_routes.py
git commit -m "feat: migrate Quality to typed StateGraphs"
```

### Task 6: Migrate Healing repair graphs and approval interrupt

**Files:**

- Create: `packages/capabilities/assurance-healing/assurance_healing/graphs/__init__.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/graphs/state.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/graphs/nodes.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/graphs/routes.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/graphs/factory.py`
- Create: `packages/capabilities/assurance-healing/tests/test_graph_factory.py`
- Create: `packages/capabilities/assurance-healing/tests/test_graph_routes.py`
- Create: `packages/capabilities/assurance-healing/tests/test_graph_interrupts.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/operations/workflow_state.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_workflow_state.py`

**Interfaces:** `repair_failure`, `repair_coverage`, two Agent contracts, pure repair-round advance, typed approval.

- [ ] **Step 1: Test independent bundle/state contracts.**

Assert exact two exports/Agent IDs and independent failure/coverage inputs. Test not-eligible, exhausted, failed, needs-review and repaired routes; unknown status fails closed.

- [ ] **Step 2: Prove round advance is pure and budgeted.**

Use the owner-local pure decision contract created in Attempt Task 4; graph calls it directly, binds no direct Task contract, and matches legacy valid/invalid results. Both repair exports derive every effectful activation from repair kind plus the parent-supplied business round/current trigger, never a fixed one-shot value. Budget exhaustion is a business terminal, not recursion failure.

- [ ] **Step 3: Test the one Feature interrupt.**

Coverage review accepts only `approve | reject`; restart reuses the same interrupt identity and performs no pre-interrupt side effect. Assert integration receipts use exactly the three Healing effect IDs `assurance.healing.effect.allocation.v2`, `assurance.healing.effect.heal-apply.v2`, and `assurance.healing.effect.proposal-approved.v1`. Effectful allocation/apply/approval work remains inside the two Agent Attempts/Kernel effect protocol, not an interrupt node.

- [ ] **Step 4: Run RED, implement, verify, commit.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-healing/tests/test_graph_factory.py \
  packages/capabilities/assurance-healing/tests/test_graph_routes.py \
  packages/capabilities/assurance-healing/tests/test_graph_interrupts.py
uv run pytest -q packages/capabilities/assurance-healing/tests
uv run lint-imports
```

Expected initially: `ModuleNotFoundError: assurance_healing.graphs`. Stage exactly:

```bash
git add \
  packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py \
  packages/capabilities/assurance-healing/assurance_healing/operations/workflow_state.py \
  packages/capabilities/assurance-healing/assurance_healing/graphs/__init__.py \
  packages/capabilities/assurance-healing/assurance_healing/graphs/state.py \
  packages/capabilities/assurance-healing/assurance_healing/graphs/nodes.py \
  packages/capabilities/assurance-healing/assurance_healing/graphs/routes.py \
  packages/capabilities/assurance-healing/assurance_healing/graphs/factory.py \
  packages/capabilities/assurance-healing/tests/test_workflow_state.py \
  packages/capabilities/assurance-healing/tests/test_graph_factory.py \
  packages/capabilities/assurance-healing/tests/test_graph_routes.py \
  packages/capabilities/assurance-healing/tests/test_graph_interrupts.py
git commit -m "feat: migrate Healing to typed StateGraphs"
```

### Task 7: Repair Retro and improvement-evaluate production contracts

**Files:**

- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/retro.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/review.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/delivery.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_retro_graph_contract.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_evaluate_graph_contract.py`

**Interfaces:** authenticated `RetroCollectInput`, three analysis slices, typed reconcile context/candidates/current projection, complete `EvaluateMemoryInput`; real handler closure before graph acceptance.

- [ ] **Step 1: Reproduce the Retro contract break with production handlers.**

Write an end-to-end test without the universal-success task fake. Current public lifecycle-only payload must fail because collect requires retro ID/window/issue/workflow/eval slices and later analyses/reconcile require authenticated context/digests/candidates.

- [ ] **Step 2: Define typed dataflow tests.**

```text
collect requires authenticated window and all three slices
eval analysis receives only the authenticated eval slice
issue/workflow analyses receive their matching slices
three analyses assemble context before reconcile
reconcile receives context, candidates and current projection
final retro Agent receives the reconciled typed value
```

Every digest/reference originates from authenticated receipt/evidence state, not ambient filesystem scan.

- [ ] **Step 3: Reproduce and close improvement-evaluate.**

The real handler requires projection, eval run ID, outcome, report/staged/baseline/target digests. Test that a lifecycle-only public payload is rejected, then build the complete typed selector and assert the real handler commits a receipt.

- [ ] **Step 4: Run RED and make the smallest contract/operation repair.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-improvement/tests/test_retro_graph_contract.py \
  packages/capabilities/assurance-improvement/tests/test_evaluate_graph_contract.py
```

Expected: current production handlers return `invalid_input` for missing fields. Do not make the test green by weakening models or returning unconditional success.

- [ ] **Step 5: Verify existing Improvement tests and commit.**

```bash
uv run pytest -q packages/capabilities/assurance-improvement/tests
git add \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py \
  packages/capabilities/assurance-improvement/assurance_improvement/operations/retro.py \
  packages/capabilities/assurance-improvement/assurance_improvement/operations/review.py \
  packages/capabilities/assurance-improvement/assurance_improvement/operations/delivery.py \
  packages/capabilities/assurance-improvement/tests/test_retro_graph_contract.py \
  packages/capabilities/assurance-improvement/tests/test_evaluate_graph_contract.py
git commit -m "fix: close Improvement graph contracts"
```

### Task 8: Migrate all seven Improvement exports

**Files:**

- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/__init__.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/state.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/nodes.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/routes.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/retro.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/delivery.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/graphs/factory.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_graph_factory.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_graph_retro.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_graph_delivery.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_graph_interrupts.py`

**Interfaces:** seven bundle fields, six Agent contracts, eight direct Task contracts at nine occurrences, one typed human interrupt; technical effects remain Kernel-local.

- [ ] **Step 1: Write exact factory inventory.**

Assert fields `archive/retro/review/evaluate/export/apply/rollback`, six Agent IDs, eight direct Task IDs, and nine direct Attempt occurrences because evaluate-memory appears in standalone evaluate and apply. No benchmark Eval or Nightly graph is exported.

- [ ] **Step 2: Test repaired Retro end to end in the graph harness.**

Trace collect → three analyses → reconcile → retro Agent with exact typed values. Use deterministic reducers for three named analysis results; no generic token list. Archive remains an independent graph.

- [ ] **Step 3: Test delivery graphs and the three Improvement effects.**

Review, export, evaluate, apply and rollback route on typed results. Apply covers auto review, human review, evaluate, apply, reject, rework, supersede and failure. Assert graph names are not treated as effect kinds; receipt traces use only `assurance.improvement.effect.archive.v1`, `assurance.improvement.effect.delivery.v1`, or `assurance.improvement.effect.promotion.v1` as emitted by registered handlers.

- [ ] **Step 4: Test the Improvement interrupt.**

Accept exactly `approve | reject | request_rework | supersede`, validate after restart, preserve interrupt ID, and execute no effect before interrupt. Pending Kernel effects produce system interrupts from Attempt nodes, not this human decision node.

- [ ] **Step 5: Run RED, implement, verify, commit.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-improvement/tests/test_graph_factory.py \
  packages/capabilities/assurance-improvement/tests/test_graph_retro.py \
  packages/capabilities/assurance-improvement/tests/test_graph_delivery.py \
  packages/capabilities/assurance-improvement/tests/test_graph_interrupts.py
uv run pytest -q packages/capabilities/assurance-improvement/tests
uv run lint-imports
```

Expected initially: `ModuleNotFoundError: assurance_improvement.graphs`. Stage exactly:

```bash
git add \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/__init__.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/state.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/nodes.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/routes.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/retro.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/delivery.py \
  packages/capabilities/assurance-improvement/assurance_improvement/graphs/factory.py \
  packages/capabilities/assurance-improvement/tests/test_graph_factory.py \
  packages/capabilities/assurance-improvement/tests/test_graph_retro.py \
  packages/capabilities/assurance-improvement/tests/test_graph_delivery.py \
  packages/capabilities/assurance-improvement/tests/test_graph_interrupts.py
git commit -m "feat: migrate Improvement to typed StateGraphs"
```

### Task 9: Freeze all six factory bundles and security/packaging rules

**Files:**

- Modify: `tests/architecture/test_python_graph_composition.py`
- Create: `tests/product/test_feature_graph_bundles.py`
- Modify: `tests/product/test_product_packaging.py`
- Modify: `tests/product/test_feature_factory_allowlist.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_plugin.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_plugin.py`
- Modify: `packages/capabilities/assurance-execution/tests/test_plugin.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_plugin.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_plugin.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_plugin.py`

**Interfaces:** exact six fixed symbols, bundle field contracts, deterministic dry build, no SUT authority, Python graph code present in wheels while YAML remains only for legacy coexistence.

- [ ] **Step 1: Test the fixed authenticated symbol list.**

```text
assurance_intake.graphs.factory:build_intake_graphs
assurance_generation.graphs.factory:build_generation_graphs
assurance_execution.graphs.factory:build_execution_graphs
assurance_quality.graphs.factory:build_quality_graphs
assurance_healing.graphs.factory:build_healing_graphs
assurance_improvement.graphs.factory:build_improvement_graphs
```

Product allowlist has exactly these owner/symbol pairs. Reject missing/extra/duplicate owner, wrong module origin, private symbol, SUT path and config-supplied symbol.

- [ ] **Step 2: Build all factories twice with spy contexts.**

Assert identical bundle/public contract digests, exact owner-scoped contract IDs, no handler/validator/runtime injection, no environment/time/random/SUT-dependent topology, and every child checkpointer `None`.

- [ ] **Step 3: Run the complete Feature gate.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_graph_factory.py \
  packages/capabilities/assurance-generation/tests/test_graph_factory.py \
  packages/capabilities/assurance-execution/tests/test_graph_factory.py \
  packages/capabilities/assurance-quality/tests/test_graph_factory.py \
  packages/capabilities/assurance-healing/tests/test_graph_factory.py \
  packages/capabilities/assurance-improvement/tests/test_graph_factory.py \
  tests/product/test_feature_graph_bundles.py \
  tests/product/test_feature_factory_allowlist.py \
  tests/architecture/test_python_graph_composition.py
uv run lint-imports
uv run pyright
```

Expected: all exit `0`.

- [ ] **Step 4: Commit bundle/security freeze.**

```bash
git add \
  tests/architecture/test_python_graph_composition.py \
  tests/product/test_feature_graph_bundles.py \
  tests/product/test_feature_factory_allowlist.py \
  tests/product/test_product_packaging.py \
  packages/capabilities/assurance-intake/tests/test_plugin.py \
  packages/capabilities/assurance-generation/tests/test_plugin.py \
  packages/capabilities/assurance-execution/tests/test_plugin.py \
  packages/capabilities/assurance-quality/tests/test_plugin.py \
  packages/capabilities/assurance-healing/tests/test_plugin.py \
  packages/capabilities/assurance-improvement/tests/test_plugin.py
git commit -m "test: freeze authenticated Feature StateGraph bundles"
```

## Feature migration exit gate

- [ ] All six factory suites and existing Capability suites pass.
- [ ] Bundle inventory is exact: Intake 2, Generation 1, Execution 2, Quality 5, Healing 2, Improvement 7.
- [ ] Effectful graph inventory is exact: 33 Agent contracts at 34 occurrences plus eight Improvement Task contracts at nine occurrences. Four pure functions cover the other 16 legacy direct occurrences.
- [ ] The five Feature-owned `join:any` sites pass first/late/replay/merge-order/repeated-epoch/current-trigger/dispatch-cursor tests; no generic token scheduler was added.
- [ ] Generation emits four `Send` values only after count validation; Intake primary/repair composite tests cover the other two legacy `min_matches` obligations without phase nodes.
- [ ] Every migrated Feature row in the exact 50-site exclusive-route inventory calls `select_exclusive_route`; zero selects its recorded `otherwise` target and multiple named matches fail closed. Remaining Product-owned rows are explicitly marked pending, never silently omitted.
- [ ] All 12 Feature human interrupts validate exact action sets after restart.
- [ ] Retro and improvement-evaluate pass real production-handler tests; Eval remains a pure offline comparator and Nightly remains outside Product graph exports.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, and focused Capability tests before requesting review.
