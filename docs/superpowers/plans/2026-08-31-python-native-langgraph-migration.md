# Python-native LangGraph Migration Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace YAML-authored Assurance Workflow topology and the custom Workflow Runtime with authenticated, Feature-owned Python `StateGraph` factories while preserving reliable Attempt commit/recovery, the 14 public entrypoints, and revision-pinned CLI resume.

**Architecture:** LangGraph becomes the only Workflow progression and checkpoint authority. `graph-engine` remains as a Boot/application/Attempt/persistence framework; each Capability wheel owns its typed subgraphs and semantic Attempt contracts; `assurance-product` owns the fixed six-factory allowlist, Agent runtime bindings, 14 root graphs, and CLI. Effectful nodes call one idempotent `AssuranceAttemptKernel`; graph state carries control data and receipt references only.

**Tech Stack:** Python 3.11, uv workspace, LangGraph `1.2.11`, LangGraph Checkpoint `4.2.0`, LangGraph SQLite Checkpoint `3.1.1`, Pydantic v2, pytest, Click, SQLite, existing authenticated wheel composition and append-only journal primitives.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`

---

## Plan Suite

This migration crosses four independently reviewable subsystems. Execute the detailed plans below; this document is the dependency map and program exit gate.

| Plan | Detailed plan | Outcome |
|---|---|---|
| Foundation | [Python-native LangGraph Foundation](./2026-08-31-python-native-langgraph-foundation.md) | Pinned dependencies, authenticated graph revisions, anchored checkpointer, runner fencing, Boot/Application skeleton |
| Attempt | [Semantic Attempt Kernel](./2026-08-31-semantic-attempt-kernel.md) | 41 effectful semantic contracts plus 4 proven-pure functions, stable Attempt identity, composite Agent execution, workspace/effect recovery, node adapter |
| Feature | [Feature StateGraph Migration](./2026-08-31-feature-stategraph-migration.md) | Six Feature graph bundles, Execution tracer, five Feature `join:any` rewrites, and all 3 `min_matches` obligations |
| Product | [LangGraph Product Cutover](./2026-08-31-langgraph-product-cutover.md) | 14 Product roots, four Product `join:any` rewrites, shadow/parity, per-entrypoint cutover, drain, deletion of YAML Runtime |

Foundation and Attempt deliberately interleave because the revision/Boot types consume core Attempt types, while the Kernel later consumes the saver/lease/Boot seams. Execute this exact safe order rather than finishing one whole child document first:

1. Integration-base preflight.
2. Foundation Task 1.
3. Attempt Task 1.
4. Foundation Tasks 2–7.
5. Attempt Tasks 2–6.
6. Foundation Tasks 8–10; Foundation Task 8 depends on Attempt Task 2.
7. Attempt Tasks 7–10; Attempt Task 10 depends on Foundation Task 8.
8. Feature Tasks 1–9.
9. Product Tasks 1–10 in document order; Task 7 prepares every consumer/primitive, Task 8 atomically switches `aa compile` to v3-only while removing YAML/module packaging and aliases, and Task 9 then deletes the compiler/custom Runtime.

The resulting dependency graph is:

```text
Foundation dependency pin → Attempt core types
               │
               ▼
Foundation revision/persistence → Attempt registry/composite/workspace
               │
               ▼
Foundation Boot/Application ↔ Attempt Kernel/node adapter
               │
               ▼
Execution tracer → remaining five Feature bundles
               │
               ▼
14 Product roots → semantic shadow → entrypoint cutover
               │
               ▼
legacy drain → YAML/compiler/planner/scheduler deletion
```

Do not start the Feature migration against provisional Kernel or checkpointer APIs. The interleaved Foundation and Attempt waves must pass their public-contract tests first. Product root work may begin after the Feature bundle interfaces freeze, but deletion cannot begin until every public entrypoint passes parity and no live legacy Invocation remains.

## Global Constraints

- Run every command from `/Users/lvqingquan/agent/assurance-agent` through `uv run` where applicable; the workspace uses pinned CPython 3.11.
- Before implementation, invoke `superpowers:using-git-worktrees` and create a clean isolated worktree from an integration-base commit containing this spec and plan suite. The current worktree has unrelated user changes; do not copy, clean, overwrite, stage, or commit them.
- `docs/` is ignored by the repository. The plan authoring handoff must force-add exactly this master, the four linked child plans and the accepted spec; the implementation preflight below verifies they are tracked before creating a worktree. Never use a broad forced add.
- The accepted inventory depends on current uncommitted Generation/Intake/Workflow changes. Before creating that worktree, the user must land those changes in an explicit integration-base commit, or approve a different base and regenerate the inventory/spec/plan. Never reconstruct or cherry-pick an inferred subset from the dirty worktree.
- Use test-driven development for every Task: add the narrow failing test, run it and record the expected failure, add the smallest implementation, rerun the focused suite, then commit only that Task's explicit paths.
- Never use `git add .`, `git add -A`, broad globs, or destructive Git commands. Every detailed plan lists exact add paths.
- Topology is direct Python `StateGraph`. Do not introduce `GraphDef` lowering, a YAML adapter, `AgentLeafSpec`, a generic token scheduler, a dynamic graph-provider registry, or a Product-wide dispatcher graph.
- The SUT and `.aa/` remain closed data only. They cannot supply Python, module paths, factory symbols, Attempt contracts, handlers, validators, effects, or topology.
- Product owns the fixed six authenticated factory references. A Capability graph may resolve only contracts authenticated for its own owner; Product is the only cross-Feature graph composer.
- `graph-engine` must not import `agent_runtime_contracts`, concrete runtime adapters, Capability packages, or Product code.
- The production validator baseline is 25 registered and zero bound. Every shipped target contract declares `validators` explicitly, including `()`. A non-empty production binding is a separately reviewed behavior change and is not introduced by this migration. One authenticated test-only core contract clone reuses the resolved Execution executor, binds `assurance.execution.validator.evidence.v1`, and gives both legacy and LangGraph paths the identical `ResourceClaims(writes=("tests", "src"))` envelope solely to prove accept/reject parity; it creates no new runtime binding and never enters contributions, locks, manifests, wheels, or production counts.
- Preserve all 99 legacy phase aliases and the old Runtime through shadow and rollback. Delete them only after the Phase 7 drain gate.
- One production Invocation is permanently pinned to one runtime kind and that runtime's authenticated build identity: legacy uses its byte-exact `InvocationLock` v2 digest; LangGraph uses `GraphRevision` plus ProductLock v3 digest. It is never advanced by both runtimes or switched in place.
- Preserve the exact 14 public entrypoint names: `intake`, `case`, `full`, `execute`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback`.
- A compiled graph is never serialized. `aa compile` emits `GraphBuildManifest`; runtime Boot authenticates the same sources and recompiles roots with the real checkpointer into `BootArtifact`.
- One Invocation has one runner lease and monotonically increasing fencing token. Checkpoint, journal, Kernel dispatch, promotion, effects, and receipt publication reject a stale token.
- Checkpoint state contains control data and receipt references, not workspaces, project files, secrets, raw sessions, unbounded event history, or service objects.
- A Feature-owned `join:any` current-trigger mismatch is a migration-stop defect with zero waivers. Keep production on its complete immutable `legacy-v2` runtime, but do not approve Feature freeze, enter Product cutover, drain legacy, embed a legacy join, or delete YAML/Runtime until the equivalent typed inbox/cursor passes.

## Program Baseline

Characterization tests must retain these facts until the final deletion commit:

| Surface | Baseline |
|---|---:|
| Legacy graphs | 64 |
| Legacy nodes / edges | 325 / 367 |
| Conditional edges | 124 |
| Exclusive-routing nodes | 50: 13 task, 20 subgraph, 11 interrupt, 6 gate |
| Subgraph nodes | 73 |
| Joins | 12: 9 `any`, 3 `all` |
| Interrupt nodes | 13 |
| Loop SCCs / maximum nesting | 7 / 5 |
| Product phase aliases | 99 |
| Phase-slot occurrences | 102 |
| Semantic Agent occurrences / contracts | 34 / 33 |
| Direct task occurrences / distinct contracts | 25 / 12 |
| Candidate semantic contracts before purity classification | 45 |
| Final effectful Attempt contracts / occurrences | 41 / 43 |
| Proven-pure direct functions / occurrences | 4 / 16 |
| Registered / currently bound validators | 25 / 0 |
| Registered effect kinds | 6 |
| Public entrypoints | 14 |

### Integration-base preflight

- [ ] Prove the accepted design and all five plans are tracked in the selected base:

```bash
git ls-files --error-unmatch \
  docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-foundation.md \
  docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md \
  docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md \
  docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md
```

Expected: all six paths print and the command exits `0`. If not, stop; do not create a worktree from a base that cannot carry the plan.

- [ ] The selected base reproduces 64 graphs, 325 nodes, 367 edges, 124 conditional edges, 50 nodes with `routing.mode: exclusive` (13 task, 20 subgraph, 11 interrupt, 6 gate), 12 joins (9 `any`, 3 `all`), the exact three `min_matches` rows and exact seven loop-SCC anchors below, 102 capability-slot occurrences, and 25 direct-task occurrences across 12 IDs.
- [ ] Run all six Capability `test_workflow_module.py` suites. On the accepted working-tree snapshot the observed baseline is `4 failed, 118 passed`: Execution, Quality, Healing and Improvement tests still assert concrete prepare/finalize capabilities after those YAML nodes moved to `capability_slot`. Land the four test/contract alignments as a reviewed prerequisite commit; do not hide them with xfail or copy dirty files into the migration worktree.
- [ ] Re-run the inventory and six suites after that prerequisite. The counts above remain exact and all legacy module tests pass before Foundation Task 1 begins.

The preflight inventory imports the assembled legacy `CompiledWorkflow`, reads `CompiledGraph.sccs`, requires each loop SCC to contain exactly one `join:any`, and compares the legacy facts and anchors below by exact equality. Target implementation disposition is a separate Feature-stage assertion because it cannot be derived from the legacy compiler:

```python
EXPECTED_LEGACY_MIN_MATCHES = (
    ("assurance.generation.workflow.graph.generation", "fanout", 4),
    ("assurance.intake.workflow.graph.case-design", "prepare", 2),
    ("assurance.intake.workflow.graph.case-design", "repair-prepare", 2),
)

TARGET_MIN_MATCHES_DISPOSITION = {
    ("assurance.generation.workflow.graph.generation", "fanout"): "langgraph_send",
    ("assurance.intake.workflow.graph.case-design", "prepare"): "composite_internal_dataflow",
    ("assurance.intake.workflow.graph.case-design", "repair-prepare"): "composite_internal_dataflow",
}

EXPECTED_LOOP_SCC_ANCHORS = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ("assurance.intake.workflow.graph.entry", "advance-join"),
    ("assurance.product.workflow.graph.product-execute", "coverage-needed"),
    ("assurance.product.workflow.graph.product-execute", "failed-join"),
)
```

If the accepted working-tree changes have been landed but the four tests have not, make this one prerequisite commit before creating the migration worktree:

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py
```

Update only these four tests to assert `prepare/finalize.capability is None` and the exact owner-local `capability_slot`; preserve the execute-slot assertions:

```bash
git add \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py
git commit -m "test: align legacy capability-slot ownership"
```

Expected after the edit: the six-suite command exits `0`. If the selected integration base does not contain the accepted YAML changes, stop and regenerate this suite instead of applying these test edits to a different topology.

The old internal node count is not a target. Composite Agent Attempts intentionally collapse prepare/execute/finalize plumbing. External behavior, semantic Attempt calls, decisions, interrupts, receipts, terminal states, crash recovery, and public contracts are the parity surface.

## Integration Checkpoints

### Checkpoint A: Foundation freeze

- [ ] `GraphRevision`, `GraphBuildManifest`, `BootArtifact`, runtime context, anchored saver, and runner-lease interfaces pass focused tests.
- [ ] All eight production wheels declare their direct pinned LangGraph dependencies and `uv.lock` is regenerated.
- [ ] A dry compile uses no checkpointer; runtime Boot reconstructs the same manifest with a host saver.
- [ ] In-memory and SQLite restart tests prove `thread_id == invocation_id`, strict serialization, journal anchoring, and fencing.
- [ ] Commit the foundation series before Feature graph factories import these interfaces.

### Checkpoint B: Attempt freeze

- [ ] The 33 Agent contracts and 8 effectful Improvement direct-task contracts resolve into immutable core contracts while all legacy aliases remain usable; the 4 other direct capability IDs are frozen as deterministic pure functions and later run as ordinary graph nodes.
- [ ] `AgentExecutionContract[InputT, AgentResultT, OutputT]` keeps provider result validation separate from finalize output validation.
- [ ] Stable Attempt keys, sealed staged writes, explicit validator ordering, durable prepare/promotion, all six effects, resource authorization, and system-interrupt ordinal replay pass crash tests.
- [ ] `AttemptNodeFactory.attempt(contract, semantic_node_id=..., activation=..., select=..., publish=...)` is the only effectful graph-node seam; activation comes only from stable typed business state.
- [ ] Commit the Attempt series before the Execution tracer switches from scripted to real Kernel execution.

### Checkpoint C: Feature bundle freeze

- [ ] Each Capability exposes one typed bundle from `assurance_<feature>.graphs.factory` and compiles with `checkpointer=None`.
- [ ] Spy build-context tests inventory the expected owner-scoped contract IDs and reject handler, validator, or runtime-binding injection at node sites.
- [ ] The five Feature-owned `join:any` replacements preserve current-trigger identity; all five are looped and cover epoch, late arrival, replay, deduplication, and dispatch cursor behavior. Every row is green with zero waivers; one failure blocks Checkpoints C–E while production remains on `legacy-v2`.
- [ ] The loop inventory is derived from assembled `CompiledGraph.sccs` and equals the exact seven `(graph_id, join:any node_id)` anchors; the five Feature rows link to green tests and the two Product rows remain explicit pending entries. Full SCC membership is retained in failure diagnostics but is not substituted for anchor equality.
- [ ] Exactly `generation/fanout` becomes one typed four-value `Send` route that fails before dispatch. Exactly Intake `case-design/prepare` and `repair-prepare` disappear into Composite internal two-consumer dataflow. No other target `Send`, generic helper, or phase-fanout shim is allowed.
- [ ] The authenticated test-only Execution clone binds `assurance.execution.validator.evidence.v1` and proves one accept/promote plus one reject/no-promote through the real LangGraph/Kernel path; all shipped contracts remain explicit `validators=()` and production stays 25 registered / 0 bound.
- [ ] Retro and `improvement-evaluate` input/output contracts close end-to-end; the latter is an effectful `evaluate-memory-improvement` Attempt that settles `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`. Nightly remains out of scope, and only the separate offline benchmark Eval comparator remains pure.
- [ ] The checked migration inventory accounts for all 50 legacy exclusive-routing nodes across task/subgraph/interrupt/gate kinds; every target route calls `select_exclusive_route(named_matches, otherwise=...)`, proves zero selects the declared fallback, and proves multiple matches fail instead of using priority `if/elif`.
- [ ] Commit the six Feature bundles before Product root cutover work.

### Checkpoint D: Product cutover

- [ ] Product imports exactly the six fixed factory symbols and builds exactly the 14 public roots.
- [ ] Product's four `join:any` paths pass zero-waiver current-trigger characterization: the two looped sites cover late arrival/replay/cursor behavior, while the two assessment exits prove predecessor mutual exclusion and impossibility of late reactivation or use the equivalent inbox/cursor fallback. Failure keeps production wholly on `legacy-v2` and blocks cutover/drain/deletion.
- [ ] Taken together, the Feature and Product gates account for all 9 legacy `join:any` sites, and the exact seven-anchor SCC inventory has no pending row.
- [ ] A test-only validator shadow case proves the same authenticated validator accepts/promotes and rejects/blocks promotion on both runtimes; no shipped Feature contract gains a binding.
- [ ] Product closes the shared 50-site exclusive-route inventory before YAML deletion; every entry points to a passing declared-fallback and multi-match-ambiguity test.
- [ ] CLI start/run/resume/status use the revision-pinned Application; multiple interrupts resume by interrupt ID and ambiguous scalar resume is rejected.
- [ ] Semantic shadow compares separate Invocations or scripted snapshots only; no production Invocation is dual-driven.
- [ ] Each public entrypoint records a reviewed parity result referencing the zero-waiver 9-site join evidence before its cutover flag changes.

### Checkpoint E: Drain and delete

- [ ] Creation of new legacy Invocations is disabled only after all 14 entrypoints pass cutover gates.
- [ ] Drain authorization rejects any missing, failed, or waived join-parity row; there is no per-site legacy exception inside a LangGraph Invocation.
- [ ] Drain authorization also rejects a changed seven-anchor SCC inventory, any deviation from the one-Generation-`Send`/two-Intake-Composite `min_matches` mapping, or missing cross-runtime test-only Validator accept/reject parity.
- [ ] The registry proves there are no active resumable legacy Invocations; unresolved legacy Invocations have an explicit operator decision and receipt.
- [ ] After the authenticated zero-active gate, atomically switch `aa compile` to ProductLock v3/`GraphBuildManifest` only and delete Workflow YAML/module packaging/phase aliases; then delete graph schema/compiler/projection DSL, old planner/token scheduler/subgraph loop, Workflow checkpoint authority, and engine settle loop in the exact order in the Product plan.
- [ ] Remove temporary runtime-selection switches after the final legacy artifact drains.
- [ ] Wheel-content tests prove no Workflow YAML or graph inventory ships.

## Cross-plan Contract Freeze

The following interface names are shared across plans. If an implementation discovers a required signature change, stop at the current commit, update the accepted spec and every affected detailed plan together, then resume; do not create parallel near-equivalent abstractions.

| Public seam | Exact authority | Frozen rule |
|---|---|---|
| `GraphRevision`, `GraphBuildManifest`, `BootArtifact` | Foundation Task 2 | Use the complete field layouts shown there; compiled graphs and callables never enter canonical projections. |
| `TaskAttemptContract`, `ResolvedAttemptContract`, `AttemptResolution` | Attempt Tasks 1–2 | Feature code declares data-only contracts; Boot alone resolves authenticated executors. |
| `BusinessActivation` | Attempt Task 1 | Frozen value object with `kind: Literal["root", "round", "trigger"]` and canonical `value`; use only typed business identity. |
| `AttemptKernelPort.execute_or_recover` | Attempt Task 8 | Async, one stable `AttemptKey`, one resolved contract, one validated input and one fenced runtime context. |
| `AttemptNodeFactory.attempt` and `CapabilityBuildContext.attempt` | Attempt Task 10 / Foundation Task 8 | Both require `contract_id`, `semantic_node_id`, `activation`, `select` and `publish`; the owner context resolves the ID internally and exposes no resolver. |
| `ProductGraphs` | Product Tasks 1–2 | Frozen `entrypoints: Mapping[str, CompiledStateGraph]` plus `contracts: Mapping[str, EntrypointGraphContract]`, with exactly 14 matching keys. |

Feature code never constructs `ResolvedAttemptContract` or chooses deployment runtime/model/provider values. The child task named above is the single copy of each concrete field layout and test fixture; this master intentionally does not publish a second, drift-prone skeleton.

## Release Evidence

At the end of every detailed plan, save focused command output in the implementation task log or PR description. Before claiming the program complete, run from the clean integration worktree:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
```

Expected: every command exits `0`; the full pytest summary has no failure, error, xpass, or unexpected skip; the wheel smoke script proves the installed wheels build all 14 Python roots and ship none of the deleted Workflow resources.

Then run the migration-specific release gate:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence \
  packages/capabilities/assurance-intake/tests/test_graph_factory.py \
  packages/capabilities/assurance-generation/tests/test_graph_factory.py \
  packages/capabilities/assurance-execution/tests/test_graph_factory.py \
  packages/capabilities/assurance-quality/tests/test_graph_factory.py \
  packages/capabilities/assurance-healing/tests/test_graph_factory.py \
  packages/capabilities/assurance-improvement/tests/test_graph_factory.py \
  tests/product/test_stategraph_entrypoints.py \
  tests/product/test_langgraph_shadow_parity.py \
  tests/product/test_python_native_cutover.py \
  tests/architecture/test_legacy_workflow_deleted.py
```

Expected: exit `0`, with all 14 entrypoints represented in the parity matrix and the deletion gate reporting zero legacy production surfaces.

### Planning interpretation for the Intake `min_matches` sites

The accepted design contains two statements that meet at the Intake `case-design` graph: it requires direct handling of all three legacy `min_matches` sites, and it also collapses prepare → Agent runtime → finalize into one semantic Attempt. The latter is the target authority. Therefore `case-design/prepare` and `case-design/repair-prepare` are characterized as fail-closed legacy fanouts, but are not recreated as LangGraph `Send` sites. Their two consumers become the fixed internal dataflow of `CompositeAttemptExecutor`, with primary and repair tests proving finalize receives both the prepared value and validated Agent result. Only `generation/fanout` remains a target graph fanout and raises `InsufficientRouteMatches` before returning any `Send` values. This interpretation prevents phase plumbing from being reintroduced under a new name.

## Program Completion Definition

This program is complete only when all four child plans are checked off, their focused and repository gates pass, all live Invocations are revision-pinned, and the old Workflow Runtime is physically absent. “New graphs work while legacy remains indefinitely” is an intermediate migration state, not completion.
