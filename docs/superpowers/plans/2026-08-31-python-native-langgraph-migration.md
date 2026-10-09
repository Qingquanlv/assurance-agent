# Python-native LangGraph Migration Program Implementation Plan

> Historical implementation plan: Attempt event/journal and action-runtime steps
> are superseded by the [Attempt checkpoint migration](2026-10-09-attempt-checkpoints.md).


> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace YAML-authored Assurance Workflow topology and the custom Workflow Runtime with authenticated, Feature-owned Python `StateGraph` factories, make strict locally validated Raw Agent execution permanent for all 33 Agent contracts, and preserve reliable commit/recovery, the 14 public entrypoints, and revision-pinned CLI resume.

**Architecture:** LangGraph becomes the only Workflow progression and checkpoint authority. `graph-engine` remains as a Boot/application/Attempt/persistence framework; each Capability wheel owns its typed subgraphs, semantic Attempt contracts, local result models, prepare/finalize handlers, and raw-file validation; `assurance-product` owns the fixed six-factory allowlist, 33 authenticated Raw Agent runtime bindings, 14 root graphs, and CLI. Effectful nodes call one idempotent `AssuranceAttemptKernel`; `ResolvedRawAgentExecutor` appends the installed JSON Schema to an ordinary OpenCode prompt, validates the one exact assistant JSON object locally, and closes prepare/runtime/finalize behind the existing graph-facing seam without adding a LangGraph node or edge.

**Tech Stack:** Python 3.11, uv workspace, LangGraph `1.2.11`, LangGraph Checkpoint `4.2.0`, LangGraph SQLite Checkpoint `3.1.1`, Pydantic v2, OpenCode raw sessions and messages, local JSON-Schema/Pydantic validation, canonical JSON/YAML codecs, pytest, Click, SQLite, authenticated wheel composition, durable compare-and-swap journals, and fenced workspace/effect primitives.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, amended by `docs/superpowers/specs/2026-09-02-raw-agent-runtime-cutover-design.md`

**2026-09-04 amendment:** `docs/superpowers/specs/2026-09-04-checkpoint-r-removal-design.md`
removes the former protected live-provider gate and all dependencies on it. Remaining migration work
uses focused deterministic suites plus the ordinary repository gate.

---

## Plan Suite

This migration crosses five independently reviewable workstreams. This document is the **only** program dependency map and exit gate. Detailed plans own their interfaces and task bodies, but none may redefine program order or create a second master plan. The cancelled Structured Artifact plans are historical records only and have no active task, checkpoint, CI, completion, or release dependency here.

| Plan | Detailed plan | Outcome |
|---|---|---|
| Foundation | [Python-native LangGraph Foundation](./2026-08-31-python-native-langgraph-foundation.md) | Pinned dependencies, authenticated graph revisions, anchored checkpointer, runner fencing, Boot/Application skeleton |
| Attempt | [Semantic Attempt Kernel](./2026-08-31-semantic-attempt-kernel.md) | 41 effectful semantic contracts plus 4 proven-pure functions, stable Attempt identity, base Agent execution, workspace/effect recovery, and the one graph-facing node adapter; hardened in place by Raw Agent Closure |
| Feature | [Feature StateGraph Migration](./2026-08-31-feature-stategraph-migration.md) | Six Feature graph bundles, Execution tracer, five Feature `join:any` rewrites, and all 3 `min_matches` obligations |
| Raw Agent closure | [Raw Agent Runtime Closure](./2026-09-02-raw-agent-runtime-closure.md) | Local result contract, 33 semantic runtime bindings, strict OpenCode parsing, and reuse of existing recovery primitives |
| Product | [LangGraph Product Cutover](./2026-08-31-langgraph-product-cutover.md) | 14 Product roots, four Product `join:any` rewrites, shadow/parity, per-entrypoint cutover, drain, deletion of YAML Runtime |

Foundation and Attempt originally interleaved because the revision/Boot types consume core Attempt types, while the Kernel later consumes the saver/lease/Boot seams. That interleave, all Feature work, and the first Product cutover slice have already been implemented. They are historical prerequisites, not tasks to replay.

### Authoritative continuation point

The continuation baseline is `feat/python-native-langgraph-migration@4a9cd197`. Preserve this completed history:

- [x] Integration-base preflight and Foundation Tasks 1–10.
- [x] Semantic Attempt Tasks 1–10.
- [x] Feature Tasks 1–9 and Checkpoint C topology/parity gates.
- [x] Product Tasks 1–4.
- [x] Product Task 5a: `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback` route new starts to `langgraph-v1`; commits `9a7ba2af`, `b6171237`, and `4a9cd197` form the accepted T5a series.

**Current state: T5a ACCEPTED; RAW AGENT CLOSURE COMPLETE; PRODUCT T5b IS NEXT.** The prior
Structured Output eligibility result is historical research only. It neither blocks Raw Agent work
nor satisfies any production gate. The remaining roots move through their Product tasks after
focused deterministic and ordinary repository tests pass.

Do not reset to the old integration base, re-execute completed tasks, or rewrite their journals, locks, checkpoints, receipts, or revision markers. The remaining work executes in this exact order:

1. Synchronize the accepted Raw Agent amendment and this corrected active plan suite onto a clean continuation worktree from `4a9cd197` or an explicitly reviewed successor; record all intervening code as an accepted baseline rather than reconstructing it.
2. Execute the [Raw Agent Runtime Closure](./2026-09-02-raw-agent-runtime-closure.md): remove provider-structured capability policy, bind all 33 contracts directly by semantic ID, install `ResolvedRawAgentExecutor`, enforce exact-object parsing, and prove raw transaction recovery with the existing journal/workspace/Kernel primitives. Re-run the already-cut T5a roots without changing their recorded Invocations or topology.
3. Build and verify Product T5b for the remaining eight thin Agent-dependent roots.
4. Build and verify Product T5c for `execute`.
5. Build and verify Product T5d for `full`.
6. After all 14 entrypoints route new starts to `langgraph-v1`, execute Product Tasks 6–10 in
   order. Task 7 migrates remaining consumers, Task 8 atomically switches `aa compile` to ProductLock
   v3-only while deleting Workflow YAML/module packaging and all 99 aliases, Task 9 deletes the
   compiler/custom Runtime after focused deletion tests and ordinary CI, and Task 10 removes
   migration selectors while retaining the revision registry and focused Raw Agent tests.

The resulting dependency graph is:

```text
completed Foundation/Attempt/Feature/Product T1–4
               │
               ▼
Product T5a: four non-Agent roots on LangGraph
               │
               ▼
Raw Agent Runtime Closure behind AttemptNodeFactory
               │
               ▼
33 contracts + 33 semantic bindings + existing production ports
               │
               ▼
T5a and Raw Agent deterministic verification
               │
               ▼
T5b implementation and verification
               │
               ▼
T5c implementation and verification
               │
               ▼
T5d implementation and verification
               │
               ▼
T6–T10 → focused deletion checks → Raw checks stay in ordinary CI
```

The existing Feature graphs and four non-Agent T5a cutovers remain valid. Legacy drain/deletion
remain blocked until T5d is complete, all 14 roots create LangGraph starts, and the existing drain,
revision, parity, crash, and lifecycle requirements pass. A failed change never rebinds an existing
revision-pinned Invocation or rolls back an accepted T5a root.

## Global Constraints

- Run every command from `/Users/lvqingquan/agent/assurance-agent` through `uv run` where applicable; the workspace uses pinned CPython 3.11.
- Before remaining implementation, invoke `superpowers:using-git-worktrees` and create a clean isolated continuation worktree from `4a9cd197` or an explicitly reviewed successor containing this corrected plan suite. The current planning worktree has unrelated user changes; do not copy, clean, overwrite, stage, or commit them.
- `docs/` is ignored by the repository. The plan authoring handoff must force-add exactly this master, its five linked active detailed plans, and both accepted active specs; the continuation preflight below verifies they are tracked before implementation resumes. Cancelled Structured records may be preserved in a separate reviewed historical-doc change but are not execution authority. Never use a broad forced add.
- The original Generation/Intake/Workflow inventory is already captured in completed migration history. Any code after `4a9cd197` enters the continuation base only through an explicit reviewed successor; never reconstruct or cherry-pick an inferred subset from the dirty planning worktree.
- The Raw Agent result schema is generated from authenticated installed `AgentResultT` code, appended last to the ordinary prompt, and reused for local validation. It is never loaded from a skill/project, sent through OpenCode `format.type == "json_schema"`, or treated as a provider capability.
- Raw Agent closure must not add an artifact registry, typed-slot ownership, Kernel JSON/YAML materializer, materialization manifest, or materialization receipt. Capability finalizers retain responsibility for parsing and validating actual raw files.
- Use test-driven development for every Task: add the narrow failing test, run it and record the expected failure, add the smallest implementation, rerun the focused suite, then commit only that Task's explicit paths.
- Never use `git add .`, `git add -A`, broad globs, or destructive Git commands. Every detailed plan lists exact add paths.
- Topology is direct Python `StateGraph`. Do not introduce `GraphDef` lowering, a YAML adapter, `AgentLeafSpec`, a generic token scheduler, a dynamic graph-provider registry, or a Product-wide dispatcher graph.
- The SUT and `.aa/` remain closed data only. They cannot supply Python, module paths, factory symbols, Attempt contracts, handlers, validators, effects, or topology.
- Product owns the fixed six authenticated factory references. A Capability graph may resolve only contracts authenticated for its own owner; Product is the only cross-Feature graph composer.
- `graph-engine` must not import `agent_runtime_contracts`, concrete runtime adapters, Capability packages, or Product code.
- The production validator baseline is 25 registered and zero bound. Every shipped target contract declares `validators` explicitly, including `()`. A non-empty production binding is a separately reviewed behavior change and is not introduced by this migration. One authenticated test-only core contract clone reuses the resolved Execution executor, binds `assurance.execution.validator.evidence.v1`, and gives both legacy and LangGraph paths the identical `ResourceClaims(writes=("tests", "src"))` envelope solely to prove accept/reject parity; it creates no new runtime binding and never enters contributions, locks, manifests, wheels, or production counts.
- Preserve all 99 legacy phase aliases and the old Runtime through shadow and rollback, but forbid Raw Agent binding or execution from consulting them. Delete the aliases in Product T8 only after the authenticated drain gate.
- One production Invocation is permanently pinned to one runtime kind and that runtime's authenticated build identity: legacy uses its byte-exact `InvocationLock` v2 digest; LangGraph uses `GraphRevision` plus ProductLock v3 digest. It is never advanced by both runtimes or switched in place.
- Preserve the exact 14 public entrypoint names: `intake`, `case`, `full`, `execute`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback`.
- A compiled graph is never serialized. `aa compile` emits `GraphBuildManifest`; runtime Boot authenticates the same sources and recompiles roots with the real checkpointer into `BootArtifact`.
- One Invocation has one runner lease and monotonically increasing fencing token. Checkpoint, journal, Kernel dispatch, promotion, effects, and receipt publication reject a stale token.
- Checkpoint state contains control data and receipt references, not workspaces, project files, secrets, raw sessions, unbounded event history, or service objects.
- A Feature-owned `join:any` current-trigger mismatch is a migration-stop defect with zero waivers. A regression blocks every not-yet-cut reachable entrypoint plus legacy drain/deletion; it never switches an existing revision-pinned Invocation in place or invalidates the four already-cut non-Agent roots without evidence that they reach the defect.
- Local Agent-result validation is unconditional. Remove `requires_provider_schema`, `provider_schema`, `requires_structured_output`, `opencode_structured_output`, and equivalent negotiation flags from the new production contract rather than pinning them false or retaining compatibility branches.
- The release candidate publishes exactly one authenticated Raw Agent runtime binding for each of the 33 semantic Agent contract IDs. Product Boot rejects missing, duplicate, extra, wrong-owner, wrong-source, alias-derived, or contract-digest-drifting bindings; the SUT, `.aa/`, environment variables, and CLI cannot supply or override one.
- One AttemptKey binds one OpenCode root session and at most one admitted prompt. Recovery adopts/observes/reconciles that activity; an unprovable admission or external completion becomes indeterminate, never an automatic redispatch. A policy-authorized business retry requires a new `BusinessActivation` and therefore a new AttemptKey.
- `prepare` and `finalize` are deterministic and cannot publish effects or mutate promotable bytes. OpenCode alone mutates Agent-authorized raw paths; the finalizer reads them through `RawFinalizeBundle`, and the Kernel retains resource authorization, output validation, seal, ordered validators, durable prepare, promote/recover, all six effects, and receipt ordering.
- Reuse the existing Attempt journal, activity, workspace, secret, fencing, and effect primitives. Do not move them or add parallel production-host abstractions during Raw Agent closure. Product T9 may delete or relocate a legacy Runtime module only after every retained consumer has an equivalent existing home and tests prove reopen still works.

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

### Original integration-base preflight — completed historical prerequisite

The checks below produced the accepted migration baseline and remain regression evidence. Do not treat them as a request to reset from `4a9cd197` or replay completed Foundation/Attempt/Feature work.

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

The old internal node count is not a target. Semantic Agent Attempts intentionally collapse prepare/activity/finalize plumbing behind `AttemptNodeFactory` and the Kernel. External behavior, semantic Attempt calls, decisions, interrupts, receipts, terminal states, crash recovery, and public contracts are the parity surface.

### Raw Agent continuation preflight — pending

- [ ] Start from `feat/python-native-langgraph-migration@4a9cd197` or an explicitly reviewed successor that contains the complete T5a series. Record the selected full SHA and prove the worktree is clean before source work.
- [ ] Prove both accepted active specs, this unique program master, and all five active detailed plans are tracked in that continuation branch:

```bash
git ls-files --error-unmatch \
  docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md \
  docs/superpowers/specs/2026-09-02-raw-agent-runtime-cutover-design.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-foundation.md \
  docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md \
  docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md \
  docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md \
  docs/superpowers/plans/2026-09-02-raw-agent-runtime-closure.md
```

Expected: all eight paths print and the command exits `0`. An ignored local copy is not execution authority. Cancelled Structured plans are not included in this active-plan proof.

- [ ] Freeze active Invocation counts by `(runtime_kind, revision_id)` and the exact pre-Raw-closure GraphRevision/ProductLock digest. Existing Invocations retain those identities and reopen through the already-planned revision registry; Raw Agent closure adds no new deployment registry or container authority.
- [ ] Verify the cutover map is exactly four `langgraph-v1` non-Agent roots and ten `legacy-v2` Agent-dependent roots. A broader cutover, global Product Boot failure, missing old-revision artifact, or a status claiming all of Task 5 is complete is a preflight failure.
- [ ] Characterize and freeze the actual Raw Agent blockers before source work: 33 contracts incorrectly require provider schema while the adapter advertises none; Product still uses deferred phases; semantic runtime facts depend on phase aliases; and terminal JSON extraction is permissive. First prove whether the existing journal/workspace authorization is insufficient; change it only where a recovery or security test fails.

**Historical Structured-probe disposition — not a preflight task:** preserve only the immutable
research record and the cancelled, visibly non-executable design/plans. Raw Agent Closure R5 deleted
`scripts/opencode_structured_output_eligibility_probe.py` and
`tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py`; neither executable file,
its result, nor Checkpoint S0 is a production capability, blocker, or qualification
for the permanent Raw path.

## Integration Checkpoints

### Checkpoint A: Foundation freeze

**Continuation status:** closed before `4a9cd197`; retain every row as a regression gate.

- [ ] `GraphRevision`, `GraphBuildManifest`, `BootArtifact`, runtime context, anchored saver, and runner-lease interfaces pass focused tests.
- [ ] All eight production wheels declare their direct pinned LangGraph dependencies and `uv.lock` is regenerated.
- [ ] A dry compile uses no checkpointer; runtime Boot reconstructs the same manifest with a host saver.
- [ ] In-memory and SQLite restart tests prove `thread_id == invocation_id`, strict serialization, journal anchoring, and fencing.
- [ ] Commit the foundation series before Feature graph factories import these interfaces.

### Checkpoint B: Attempt freeze

**Continuation status:** closed for the original Attempt interface before `4a9cd197`. Raw Agent Closure hardens production execution behind this seam; it does not reopen or invalidate the historical task sequence.

- [ ] The 33 Agent contracts and 8 effectful Improvement direct-task contracts resolve into immutable core contracts while all legacy aliases remain usable; the 4 other direct capability IDs are frozen as deterministic pure functions and later run as ordinary graph nodes.
- [ ] `AgentExecutionContract[InputT, AgentResultT, OutputT]` keeps strict locally validated Agent-result parsing/model validation separate from finalize output validation; provider-side schema enforcement is not part of the contract.
- [ ] Stable Attempt keys, sealed staged writes, explicit validator ordering, durable prepare/promotion, all six effects, resource authorization, and system-interrupt ordinal replay pass crash tests.
- [ ] `AttemptNodeFactory.attempt(contract, semantic_node_id=..., activation=..., select=..., publish=...)` is the only effectful graph-node seam; activation comes only from stable typed business state.
- [ ] Commit the Attempt series before the Execution tracer switches from scripted to real Kernel execution.

### Checkpoint C: Feature bundle freeze

**Continuation status:** closed before `4a9cd197`; existing Python `StateGraph` factories are retrofit consumers, not future tasks.

- [ ] Each Capability exposes one typed bundle from `assurance_<feature>.graphs.factory` and compiles with `checkpointer=None`.
- [ ] Spy build-context tests inventory the expected owner-scoped contract IDs and reject handler, validator, or runtime-binding injection at node sites.
- [ ] The five Feature-owned `join:any` replacements preserve current-trigger identity; all five are looped and cover epoch, late arrival, replay, deduplication, and dispatch cursor behavior. Every row is green with zero waivers; a regression blocks each not-yet-cut reachable root plus Checkpoints R–E without switching existing Invocations in place.
- [ ] The loop inventory is derived from assembled `CompiledGraph.sccs` and equals the exact seven `(graph_id, join:any node_id)` anchors; the five Feature rows link to green tests and the two Product rows remain explicit pending entries. Full SCC membership is retained in failure diagnostics but is not substituted for anchor equality.
- [ ] Exactly `generation/fanout` becomes one typed four-value `Send` route that fails before dispatch. Exactly Intake `case-design/prepare` and `repair-prepare` disappear into fixed Feature-owned semantic-Attempt two-consumer dataflow, served by `ResolvedRawAgentExecutor`. No other target `Send`, generic helper, compatibility executor, or phase-fanout shim is allowed.
- [ ] The authenticated test-only Execution clone binds `assurance.execution.validator.evidence.v1` and proves one accept/promote plus one reject/no-promote through the real LangGraph/Kernel path; all shipped contracts remain explicit `validators=()` and production stays 25 registered / 0 bound.
- [ ] Retro and `improvement-evaluate` input/output contracts close end-to-end; the latter is an effectful `evaluate-memory-improvement` Attempt that settles `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`. Nightly remains out of scope, and only the separate offline benchmark Eval comparator remains pure.
- [ ] The checked migration inventory accounts for all 50 legacy exclusive-routing nodes across task/subgraph/interrupt/gate kinds; every target route calls `select_exclusive_route(named_matches, otherwise=...)`, proves zero selects the declared fallback, and proves multiple matches fail instead of using priority `if/elif`.
- [ ] Commit the six Feature bundles before Product root cutover work.

### Raw Agent production closure

**Continuation status:** complete. Ordinary tests retain the exact semantic contract/binding and
graph-occurrence inventories, strict terminal JSON/local validation, crash/workspace/fencing/Kernel
coverage, and all six Effect paths. The repository does not certify an external OpenCode binary,
provider, or model.

### Checkpoint D: Product cutover

**Continuation status:** Product T5a is closed for four non-Agent roots and Raw Agent production
closure is complete; T5b is next. T5b, T5c, and T5d each require their focused Product, Raw Agent,
recovery, and lifecycle tests plus the ordinary repository gate.

- [ ] Product imports exactly the six fixed factory symbols and builds exactly the 14 public roots.
- [ ] Product's four `join:any` paths pass zero-waiver current-trigger characterization: the two looped sites cover late arrival/replay/cursor behavior, while the two assessment exits prove predecessor mutual exclusion and impossibility of late reactivation or use the equivalent inbox/cursor fallback. Failure blocks each not-yet-cut reachable root plus drain/deletion; it does not silently switch an existing Invocation or unrelated T5a root.
- [ ] Taken together, the Feature and Product gates account for all 9 legacy `join:any` sites, and the exact seven-anchor SCC inventory has no pending row.
- [ ] A test-only validator shadow case proves the same authenticated validator accepts/promotes and rejects/blocks promotion on both runtimes; no shipped Feature contract gains a binding.
- [ ] Product closes the shared 50-site exclusive-route inventory before YAML deletion; every entry points to a passing declared-fallback and multi-match-ambiguity test.
- [ ] CLI start/run/resume/status use the revision-pinned Application; multiple interrupts resume by interrupt ID and ambiguous scalar resume is rejected.
- [ ] Semantic shadow compares separate Invocations or scripted snapshots only; no production Invocation is dual-driven.
- [ ] Each public entrypoint records a reviewed parity result referencing the zero-waiver 9-site join evidence before its cutover flag changes.
- [ ] The cutover journal proves the exact order `T5b → T5c → T5d`; selectors affect only future
  starts and never rebind an existing Invocation. Each tranche passes its focused tests and ordinary
  repository gate before the next begins.

### Checkpoint E: Drain and delete

**Continuation status:** pending; depends on completed T5d and 14/14 new-start cutover.

- [ ] Creation of new legacy Invocations is disabled only after all 14 entrypoints pass cutover gates.
- [ ] Drain authorization rejects any missing, failed, or waived join-parity row; there is no per-site legacy exception inside a LangGraph Invocation.
- [ ] Drain authorization also rejects a changed seven-anchor SCC inventory, any deviation from the one-Generation-`Send`/two-Intake-Composite `min_matches` mapping, or missing cross-runtime test-only Validator accept/reject parity.
- [ ] The registry proves there are no active resumable legacy Invocations; unresolved legacy Invocations have an explicit operator decision and receipt.
- [ ] Revision retention is checked independently from legacy drain: a pre-Raw-closure or earlier LangGraph deployment artifact cannot retire while any resumable Invocation remains pinned to it, even after legacy runtime deletion is authorized.
- [ ] After the authenticated zero-active gate, T7 first migrates every retained production primitive and consumer out of deletion-bound modules. T8 then atomically switches `aa compile` to ProductLock v3/`GraphBuildManifest` only and deletes Workflow YAML/module packaging/all 99 phase aliases. T9 deletes graph schema/compiler/projection DSL, old planner/token scheduler/subgraph loop, Workflow checkpoint authority, engine settle loop, and custom Runtime only after focused deletion tests and ordinary CI pass.
- [ ] Remove temporary runtime-selection switches after the final legacy artifact drains.
- [ ] Wheel-content tests prove no Workflow YAML or graph inventory ships.
- [ ] T10 retains ProductLock v3, GraphRevision/deployment retention, 33 semantic contracts/bindings,
  permanent Attempt/activity/workspace/effect modules, and focused Raw Agent tests while removing
  migration-only selectors and any Structured-only code with no remaining consumer.

## Cross-plan Contract Freeze

The following interface names are shared across plans. If an implementation discovers a required signature change, stop at the current commit, update the accepted spec and every affected detailed plan together, then resume; do not create parallel near-equivalent abstractions.

| Public seam | Exact authority | Frozen rule |
|---|---|---|
| `GraphRevision`, `GraphBuildManifest`, `BootArtifact` | Foundation Task 2 | Use the complete field layouts shown there; compiled graphs and callables never enter canonical projections. |
| `TaskAttemptContract`, `ResolvedAttemptContract`, `AttemptResolution` | Attempt Tasks 1–2 | Feature code declares data-only contracts; Boot alone resolves authenticated executors. |
| `BusinessActivation` | Attempt Task 1 | Frozen value object with `kind: Literal["root", "round", "trigger"]` and canonical `value`; use only typed business identity. |
| `AgentExecutionContract[InputT, AgentResultT, OutputT]` and `AgentExecutionContractProjectionV1` | Attempt Task 3, finalized by Raw Agent Closure | Feature owns inputs/results/outputs, handlers, resources, path policy, validators, timeout and retry. The closed projection uses `schema_version == "raw-agent-contract-v1"`; local result validation is unconditional. |
| `RawAgentRuntimeBindingProjectionV1` | Raw Agent Runtime Closure | Product owns only runtime/adapter/provider/model/policy/secret handles/activity recovery, references the exact Feature contract digest, uses `schema_version == "raw-agent-runtime-binding-v1"`, and supplies exactly 33 canonically ordered rows. |
| `ResultContract`, `AgentRunResult`, `RawFinalizeBundle`, `ResolvedRawAgentExecutor` | Raw Agent Runtime Closure | Delivery mode is `assistant_json_local_v1`; business payload is `result_payload`; active source has no `extraction_mode == "structured"` or `structured_result` alias; every finalizer receives validated input, prepared value, validated result, complete run evidence, and a read-only raw workspace through one closed bundle. |
| `BoundAttemptDispatch` and `AttemptKernelPort.execute_or_recover` | Attempt Tasks 8–10, hardened by Raw Agent Closure | Node adapter binds validated input, resources/path policy, activation and authenticated contract/binding closure before key derivation; the Kernel accepts the frozen dispatch and fenced runtime context, drives initial execution or in-flight reconcile, and resolves none of those authorities again. |
| `AttemptNodeFactory.attempt` and `CapabilityBuildContext.attempt` | Attempt Task 10 / Foundation Task 8, retained by Raw Agent Closure | Existing graph-facing interface remains the only effectful node seam. Raw session transport, strict local validation and finalization stay behind it and add zero graph nodes. |
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

The ordinary repository gate includes Raw Agent contract/binding inventory, lifecycle,
transaction/recovery, and negative provider-structured/typed-materializer tests. After T10 these
focused tests remain part of normal CI; they make no live provider/model claim.

### Planning interpretation for the Intake `min_matches` sites

The accepted design contains two statements that meet at the Intake `case-design` graph: it requires direct handling of all three legacy `min_matches` sites, and it also collapses prepare → Agent runtime → finalize into one semantic Attempt. The latter is the target authority. Therefore `case-design/prepare` and `case-design/repair-prepare` are characterized as fail-closed legacy fanouts, but are not recreated as LangGraph `Send` sites. Their two consumers become fixed Feature-owned prepare/finalize dataflow behind `ResolvedRawAgentExecutor`, with primary and repair tests proving the one `RawFinalizeBundle` contains validated input, prepared value, validated Agent result, complete run evidence, and read-only raw workspace. Only `generation/fanout` remains a target graph fanout and raises `InsufficientRouteMatches` before returning any `Send` values. This interpretation prevents phase plumbing from being reintroduced under a new name.

## Program Completion Definition

This program is complete only when all five active workstreams are checked off; six authenticated
Python Feature factories build all 14 Product LangGraph roots; all 33 semantic Agent contracts and
bindings pass strict local JSON/result/file validation and one-session recovery tests; the one
`AssuranceAttemptKernel` transaction preserves authorization, seal, validators, durable prepare,
promotion, six effects, receipt and fencing; every live Invocation remains revision-pinned; and
Workflow YAML, all 99 phase aliases, the compiler/planner/scheduler/settle loop, custom Graph Runtime,
provider-structured capability promises, typed materializers, and materialization receipts are
absent. “New graphs work while legacy remains indefinitely” is an intermediate migration state, not
completion.
