# Python-native LangGraph Migration Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace YAML-authored Assurance Workflow topology and the custom Workflow Runtime with authenticated, Feature-owned Python `StateGraph` factories, upgrade every Agent Attempt to the Structured Artifact Pipeline, and preserve reliable commit/recovery, the 14 public entrypoints, and revision-pinned CLI resume.

**Architecture:** LangGraph becomes the only Workflow progression and checkpoint authority. `graph-engine` remains as a Boot/application/Attempt/persistence framework; each Capability wheel owns its typed subgraphs, semantic Attempt contracts, and artifact projections; `assurance-product` owns the fixed six-factory allowlist, Agent runtime bindings, 14 root graphs, and CLI. Effectful nodes call one idempotent `AssuranceAttemptKernel`; the Structured Artifact Pipeline is retrofitted behind that existing graph-facing seam and adds no LangGraph node or edge.

**Tech Stack:** Python 3.11, uv workspace, LangGraph `1.2.11`, LangGraph Checkpoint `4.2.0`, LangGraph SQLite Checkpoint `3.1.1`, Pydantic v2, OpenCode Structured Output, canonical JSON/YAML codecs, pytest, Click, SQLite, existing authenticated wheel composition and append-only journal primitives.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, extended by `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`

---

## Plan Suite

This migration crosses five independently reviewable workstreams. This document is the **only** program dependency map and exit gate. Detailed plans own their interfaces and task bodies, but none may redefine program order or create a second master plan.

| Plan | Detailed plan | Outcome |
|---|---|---|
| Foundation | [Python-native LangGraph Foundation](./2026-08-31-python-native-langgraph-foundation.md) | Pinned dependencies, authenticated graph revisions, anchored checkpointer, runner fencing, Boot/Application skeleton |
| Attempt | [Semantic Attempt Kernel](./2026-08-31-semantic-attempt-kernel.md) | 41 effectful semantic contracts plus 4 proven-pure functions, stable Attempt identity, base Agent execution, workspace/effect recovery, node adapter; extended in place by Checkpoint S |
| Feature | [Feature StateGraph Migration](./2026-08-31-feature-stategraph-migration.md) | Six Feature graph bundles, Execution tracer, five Feature `join:any` rewrites, and all 3 `min_matches` obligations |
| Structured retrofit | [Structured Artifact Pipeline Resume Tranche](./2026-09-01-structured-artifact-pipeline.md) | Checkpoint S0 exact-release eligibility probe, then—only when S0 is green—OpenCode Structured Output transport, provider-neutral artifact contracts, deterministic materialization, durable structured recovery, and migration of 33 Agent contracts/34 occurrences |
| Product | [LangGraph Product Cutover](./2026-08-31-langgraph-product-cutover.md) | 14 Product roots, four Product `join:any` rewrites, shadow/parity, per-entrypoint cutover, drain, deletion of YAML Runtime |

Foundation and Attempt originally interleaved because the revision/Boot types consume core Attempt types, while the Kernel later consumes the saver/lease/Boot seams. That interleave, all Feature work, and the first Product cutover slice have already been implemented. They are historical prerequisites, not tasks to replay.

### Authoritative continuation point

The continuation baseline is `feat/python-native-langgraph-migration@4a9cd197`. Preserve this completed history:

- [x] Integration-base preflight and Foundation Tasks 1–10.
- [x] Semantic Attempt Tasks 1–10.
- [x] Feature Tasks 1–9 and Checkpoint C topology/parity gates.
- [x] Product Tasks 1–4.
- [x] Product Task 5a: `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback` route new starts to `langgraph-v1`; commits `9a7ba2af`, `b6171237`, and `4a9cd197` form the accepted T5a series.

**Current state: PARKED AT CHECKPOINT S0.** Official OpenCode v1.18.26 is a valid negative fixture. Authorized now: plan synchronization and OpenCode Task 0 only. While S0 is red, OpenCode Tasks 1–7, Artifact Tasks 1–10, Capability Tasks 1–12, Structured R1–R6, and Product T5b–T10 remain deferred.

Do not reset to the old integration base, re-execute completed tasks, or rewrite their journals, locks, checkpoints, receipts, or revision markers. The remaining work executes in this exact order:

1. Synchronize this corrected plan suite and the Structured Artifact spec/research onto a clean continuation worktree from `4a9cd197` or an explicitly reviewed successor; record all intervening code as an accepted baseline rather than reconstructing it.
2. Execute OpenCode Task 0 only and close **Checkpoint S0: OpenCode Release Eligibility** for one exact official release. If S0 is red, stop the Structured tranche here: do not start R1–R6, Product T5b–T5d, drain, or deletion.
3. Only after S0 is green, execute the Structured Artifact Pipeline resume tranche R1–R3: migrate the **existing** executor/Kernel/33 contracts and adapt the already-existing Feature/Product factories without changing their topology.
4. Execute R4: re-run the complete Foundation/Attempt/Feature regression gates plus Product Tasks 1–4 and coexistence tests; this is re-certification of implemented behavior, not task replay.
5. Execute R5: run the complete exact-release OpenCode qualification and close **Checkpoint S: Structured Agent Production Gate** on the final integrated candidate revision. S0 evidence is only an investment gate and cannot satisfy or be promoted by Checkpoint S.
6. Execute Product Task 5b for the remaining eight thin Agent-dependent roots, Task 5c for `execute`, and Task 5d for `full`. Checkpoint S blocks these ten future-start cutovers only.
7. After all 14 entrypoints route new starts to `langgraph-v1`, execute Product Tasks 6–10 in order. Task 7 prepares every consumer/primitive, Task 8 atomically switches `aa compile` to v3-only while removing YAML/module packaging and aliases, and Task 9 then deletes the compiler/custom Runtime.

The resulting dependency graph is:

```text
completed Foundation/Attempt/Feature/Product T1–4
               │
               ▼
Product T5a: four non-Agent roots on LangGraph
               │
               ▼
OpenCode O0 exact-release eligibility probe → Checkpoint S0
               │
               ├── red: freeze Structured R1–R6; retain 4 LangGraph / 10 legacy
               │
               └── green
                     │
                     ▼
Structured executor/Kernel/artifact retrofit behind AttemptNodeFactory
               │
               ▼
33 Agent contracts + existing Feature/Product factory re-certification
               │
               ▼
exact OpenCode release qualification → Checkpoint S
               │
               ▼
Product T5b/T5c/T5d → legacy drain → YAML/compiler/Runtime deletion
```

The existing Feature graphs and four non-Agent cutovers remain valid while S0 is red or the later retrofit is underway. S0 red freezes all Structured implementation after Task 0; Checkpoint S later blocks Agent-dependent cutover, legacy drain, and deletion until the fully implemented pipeline is certified. Neither gate may globally fail Product Boot or roll back a root whose reachable contract set contains no Agent Attempt.

## Global Constraints

- Run every command from `/Users/lvqingquan/agent/assurance-agent` through `uv run` where applicable; the workspace uses pinned CPython 3.11.
- Before remaining implementation, invoke `superpowers:using-git-worktrees` and create a clean isolated continuation worktree from `4a9cd197` or an explicitly reviewed successor containing this corrected plan suite. The current planning worktree has unrelated user changes; do not copy, clean, overwrite, stage, or commit them.
- `docs/` is ignored by the repository. The plan authoring handoff must force-add exactly this master, its eight linked detailed plans, both accepted specs, and the OpenCode source audit; the continuation preflight below verifies they are tracked before implementation resumes. Never use a broad forced add.
- The original Generation/Intake/Workflow inventory is already captured in completed migration history. Any code after `4a9cd197` enters the continuation base only through an explicit reviewed successor; never reconstruct or cherry-pick an inferred subset from the dirty planning worktree.
- Before Checkpoint S0 is green, the only authorized Structured source task is OpenCode Task 0's standalone exact-release eligibility probe. Do not create the provider-neutral activity seam, modify the production OpenCode adapter, extend the Kernel, add artifact registries/materializers, or migrate any of the 33 Agent contracts speculatively.
- Checkpoint S0 evidence is non-promotable planning evidence. It never advertises `opencode_structured_output`, never satisfies a production binding, and never substitutes for OpenCode Tasks 4–7 or the full 33-row Checkpoint S certification.
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
- A Feature-owned `join:any` current-trigger mismatch is a migration-stop defect with zero waivers. A regression blocks every not-yet-cut reachable entrypoint plus legacy drain/deletion; it never switches an existing revision-pinned Invocation in place or invalidates the four already-cut non-Agent roots without evidence that they reach the defect.
- The generic Feature requirement is `requires_structured_output`; the selected OpenCode binding may satisfy it only with a promoted `opencode_structured_output` certification for the exact server/adapter/provider/model/schema matrix. `provider_schema`, `requires_provider_schema`, and provider-native `response_format` are not this interface.
- Product Boot may compile and authenticate all 14 roots while an Agent capability is absent. Fail closed when selecting/cutting over a root whose reachable Agent contract requires the absent capability, and before dispatch of such an Attempt; do not turn one unavailable Agent binding into a global Boot failure for direct/non-Agent roots.

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

The old internal node count is not a target. Semantic Agent Attempts intentionally collapse prepare/activity/materialize/finalize plumbing behind `AttemptNodeFactory` and the Kernel. External behavior, semantic Attempt calls, decisions, interrupts, receipts, terminal states, crash recovery, and public contracts are the parity surface.

### Structured continuation and Checkpoint S0 preflight — pending

- [ ] Start from `feat/python-native-langgraph-migration@4a9cd197` or an explicitly reviewed successor that contains the complete T5a series. Record the selected full SHA and prove the worktree is clean before source work.
- [ ] Prove both accepted specs, this unique program master, all eight detailed plans, and the pinned OpenCode source audit are tracked in that continuation branch:

```bash
git ls-files --error-unmatch \
  docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md \
  docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md \
  docs/research/2026-09-02-opencode-current-structured-output-source-audit.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md \
  docs/superpowers/plans/2026-08-31-python-native-langgraph-foundation.md \
  docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md \
  docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md \
  docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md \
  docs/superpowers/plans/2026-09-01-structured-artifact-pipeline.md \
  docs/superpowers/plans/2026-09-01-opencode-structured-output-gate.md \
  docs/superpowers/plans/2026-09-01-artifact-kernel-foundation.md \
  docs/superpowers/plans/2026-09-01-agent-artifact-contract-migration.md
```

Expected: all twelve paths print and the command exits `0`. The four `2026-09-01` plan files are currently ignored unless explicitly force-added; an ignored local copy is not execution authority.

- [ ] Freeze active Invocation counts by `(runtime_kind, revision_id)` and the exact pre-Structured GraphRevision/ProductLock/deployment-artifact digest. Existing Invocations retain those identities and can reopen only through that original artifact/container.
- [ ] Verify the cutover map is exactly four `langgraph-v1` non-Agent roots and ten `legacy-v2` Agent-dependent roots. A broader cutover, global Product Boot failure, missing old-revision artifact, or a status claiming all of Task 5 is complete is a preflight failure.
- [ ] Record OpenCode `v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661` as `message-roundtrip-red`: `prompt_async + noReply + format.json_schema` admits with `204`, but V1 list and single-message reads return `400`. Do not spend provider quota or advertise `opencode_structured_output` until this fail-fast probe passes for a later exact release.

## Integration Checkpoints

### Checkpoint A: Foundation freeze

**Continuation status:** closed before `4a9cd197`; retain every row as a regression gate.

- [ ] `GraphRevision`, `GraphBuildManifest`, `BootArtifact`, runtime context, anchored saver, and runner-lease interfaces pass focused tests.
- [ ] All eight production wheels declare their direct pinned LangGraph dependencies and `uv.lock` is regenerated.
- [ ] A dry compile uses no checkpointer; runtime Boot reconstructs the same manifest with a host saver.
- [ ] In-memory and SQLite restart tests prove `thread_id == invocation_id`, strict serialization, journal anchoring, and fencing.
- [ ] Commit the foundation series before Feature graph factories import these interfaces.

### Checkpoint B: Attempt freeze

**Continuation status:** closed for the original Attempt interface before `4a9cd197`. Checkpoint S extends the implementation behind this seam; it does not reopen or invalidate the historical task sequence.

- [ ] The 33 Agent contracts and 8 effectful Improvement direct-task contracts resolve into immutable core contracts while all legacy aliases remain usable; the 4 other direct capability IDs are frozen as deterministic pure functions and later run as ordinary graph nodes.
- [ ] `AgentExecutionContract[InputT, AgentResultT, OutputT]` keeps provider result validation separate from finalize output validation.
- [ ] Stable Attempt keys, sealed staged writes, explicit validator ordering, durable prepare/promotion, all six effects, resource authorization, and system-interrupt ordinal replay pass crash tests.
- [ ] `AttemptNodeFactory.attempt(contract, semantic_node_id=..., activation=..., select=..., publish=...)` is the only effectful graph-node seam; activation comes only from stable typed business state.
- [ ] Commit the Attempt series before the Execution tracer switches from scripted to real Kernel execution.

### Checkpoint C: Feature bundle freeze

**Continuation status:** closed before `4a9cd197`; existing Python `StateGraph` factories are retrofit consumers, not future tasks.

- [ ] Each Capability exposes one typed bundle from `assurance_<feature>.graphs.factory` and compiles with `checkpointer=None`.
- [ ] Spy build-context tests inventory the expected owner-scoped contract IDs and reject handler, validator, or runtime-binding injection at node sites.
- [ ] The five Feature-owned `join:any` replacements preserve current-trigger identity; all five are looped and cover epoch, late arrival, replay, deduplication, and dispatch cursor behavior. Every row is green with zero waivers; a regression blocks each not-yet-cut reachable root plus Checkpoints S–E without switching existing Invocations in place.
- [ ] The loop inventory is derived from assembled `CompiledGraph.sccs` and equals the exact seven `(graph_id, join:any node_id)` anchors; the five Feature rows link to green tests and the two Product rows remain explicit pending entries. Full SCC membership is retained in failure diagnostics but is not substituted for anchor equality.
- [ ] Exactly `generation/fanout` becomes one typed four-value `Send` route that fails before dispatch. Exactly Intake `case-design/prepare` and `repair-prepare` disappear into fixed Feature-owned semantic-Attempt two-consumer dataflow, later served by `ResolvedStructuredAgentExecutor`. No other target `Send`, generic helper, compatibility executor, or phase-fanout shim is allowed.
- [ ] The authenticated test-only Execution clone binds `assurance.execution.validator.evidence.v1` and proves one accept/promote plus one reject/no-promote through the real LangGraph/Kernel path; all shipped contracts remain explicit `validators=()` and production stays 25 registered / 0 bound.
- [ ] Retro and `improvement-evaluate` input/output contracts close end-to-end; the latter is an effectful `evaluate-memory-improvement` Attempt that settles `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`. Nightly remains out of scope, and only the separate offline benchmark Eval comparator remains pure.
- [ ] The checked migration inventory accounts for all 50 legacy exclusive-routing nodes across task/subgraph/interrupt/gate kinds; every target route calls `select_exclusive_route(named_matches, otherwise=...)`, proves zero selects the declared fallback, and proves multiple matches fail instead of using priority `if/elif`.
- [ ] Commit the six Feature bundles before Product root cutover work.

### Checkpoint S0: OpenCode Release Eligibility

**Continuation status:** pending. Execute only OpenCode Task 0 after the continuation preflight. No Structured R1–R6 source work is authorized while this checkpoint is red.

- [ ] A trusted operator records one exact official OpenCode release tag/commit/asset, platform/architecture, local candidate-binary digest, direct loopback endpoint, local data-root identity, workspace `directory` query scope, provider/model, and minimal canary schema. This is explicitly operator-trusted eligibility evidence, not authenticated serving-process identity; `current`, `latest`, version ranges, source-only feature detection, gateways, and hand-edited pass flags are rejected.
- [ ] Before any provider call, a no-model request proves `prompt_async + noReply + format.json_schema == 204`, V1 message list `== 200`, and V1 single-message `== 200`. Both successful `WithParts.info.format` reads retain canonical-equal `type/schema` and the only server-added field is required `retryCount == 2`; the request omits `retryCount`. Official `v1.18.26` remains the fixed `204/400/400` negative fixture and must consume zero provider prompts.
- [ ] Exactly one real provider/model canary reaches a terminal assistant message with no error and a schema-valid object read only from `info.structured`; text JSON, tool input, guessed structured fields, a missing/wrong canary, or an oversized result fails.
- [ ] After the trusted operator restarts the declared local candidate with the same OpenCode data root and workspace `directory` query scope, a fresh probe process re-reads the same session, user message, assistant message, and structured-candidate digest through both V1 list and single-message endpoints. The post-restart phase has no POST-capable path. The report labels this `operator_executed_unverified`; only the later controller-backed gate may claim restart proof.
- [ ] The canonical pre-state/final eligibility reports contain no password or Authorization value and are explicitly non-promotable: they cannot create an installed certification record, cannot be consumed by Product Boot, and cannot advertise `opencode_structured_output`. Passing S0 proves only that implementation investment may begin; it does not prove authenticated server identity, in-flight activity recovery, boundary isolation, all 33 schemas, or Checkpoint S.
- [ ] If any row is red, retain the accepted map of four T5a roots on `langgraph-v1` and ten Agent-dependent roots on `legacy-v2`; defer Structured R1–R6, Product T5b–T5d, drain, YAML/compiler deletion, and custom Runtime deletion until a later exact official release passes Task 0.

### Checkpoint S: Structured Agent Production Gate

**Continuation status:** pending and blocked on Checkpoint S0. After S0 is green, execute the [Structured Artifact Pipeline Resume Tranche](./2026-09-01-structured-artifact-pipeline.md) after Product T5a and before T5b.

- [ ] The existing `CompositeAttemptExecutor` implementation has been migrated in place to the closed `ResolvedDirectExecutor | ResolvedStructuredAgentExecutor` model; no second Kernel, graph-facing adapter, or compatibility executor remains authoritative.
- [ ] All 33 Agent contracts/34 semantic occurrences/69 artifact slots close through authenticated `AgentResultT`, deterministic typed materialization or explicit raw authority, structured recovery, and the unchanged validator/prepare/promote/effect/receipt tail.
- [ ] The already-existing Feature and Product factories emit the exact live `AttemptSiteCatalog`; live sites, Artifact contract domain, and frozen inventory are set-equal without adding or removing topology.
- [ ] A final integrated candidate—not the S0 canary or a pre-Feature snapshot—passes the exact OpenCode release gate for `opencode_structured_output`, including a fresh no-model `204 -> 200 list -> 200 single` fail-fast probe, controller-proven recovery, and the complete 33-row matrix. S0 output is never promoted or counted as a passing row.
- [ ] Product composition proves a missing structured capability blocks only a selected reachable Agent Attempt/cutover. The four T5a non-Agent roots still start and recover; the remaining ten roots stay `legacy-v2` until this checkpoint closes.
- [ ] A pre-S Invocation reopens only with its recorded pre-S GraphRevision/ProductLock/deployment artifact. The post-S source/ProductLock produces a distinct GraphRevision; its manifest authenticates the structured Attempt digests, and only new starts bind that revision.
- [ ] The protected Checkpoint S aggregate gate and complete repository gate pass for one immutable `candidate_sha`. Any later change to the Adapter, structured schemas/contracts, Kernel, Product binding closure, or relevant Feature factory invalidates that evidence and requires re-certification.

### Checkpoint D: Product cutover

**Continuation status:** Product T5a is closed for four non-Agent roots; T5b–T5d are pending and depend on Checkpoint S.

- [ ] Product imports exactly the six fixed factory symbols and builds exactly the 14 public roots.
- [ ] Product's four `join:any` paths pass zero-waiver current-trigger characterization: the two looped sites cover late arrival/replay/cursor behavior, while the two assessment exits prove predecessor mutual exclusion and impossibility of late reactivation or use the equivalent inbox/cursor fallback. Failure blocks each not-yet-cut reachable root plus drain/deletion; it does not silently switch an existing Invocation or unrelated T5a root.
- [ ] Taken together, the Feature and Product gates account for all 9 legacy `join:any` sites, and the exact seven-anchor SCC inventory has no pending row.
- [ ] A test-only validator shadow case proves the same authenticated validator accepts/promotes and rejects/blocks promotion on both runtimes; no shipped Feature contract gains a binding.
- [ ] Product closes the shared 50-site exclusive-route inventory before YAML deletion; every entry points to a passing declared-fallback and multi-match-ambiguity test.
- [ ] CLI start/run/resume/status use the revision-pinned Application; multiple interrupts resume by interrupt ID and ambiguous scalar resume is rejected.
- [ ] Semantic shadow compares separate Invocations or scripted snapshots only; no production Invocation is dual-driven.
- [ ] Each public entrypoint records a reviewed parity result referencing the zero-waiver 9-site join evidence before its cutover flag changes.

### Checkpoint E: Drain and delete

**Continuation status:** pending; depends on Checkpoint S and Product T5d (14/14 new-start cutover).

- [ ] Creation of new legacy Invocations is disabled only after all 14 entrypoints pass cutover gates.
- [ ] Drain authorization rejects any missing, failed, or waived join-parity row; there is no per-site legacy exception inside a LangGraph Invocation.
- [ ] Drain authorization also rejects a changed seven-anchor SCC inventory, any deviation from the one-Generation-`Send`/two-Intake-Composite `min_matches` mapping, or missing cross-runtime test-only Validator accept/reject parity.
- [ ] The registry proves there are no active resumable legacy Invocations; unresolved legacy Invocations have an explicit operator decision and receipt.
- [ ] Revision retention is checked independently from legacy drain: a pre-S LangGraph deployment artifact cannot retire while any resumable Invocation remains pinned to it, even after legacy runtime deletion is authorized.
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
| `StructuredAgentActivityPort` | Structured OpenCode Task 1 | Provider-neutral dispatch/adopt/observe seam; OpenCode-specific capability names never enter core or Feature code. |
| `ArtifactContract`, `ArtifactSlot`, `MaterializationEntryReceipt`, `MaterializationReceipt` | Structured Artifact Tasks 1–4 | The only artifact authority and deterministic materialization receipt family. |
| `BoundAttemptDispatch` and `AttemptKernelPort.execute_or_recover` | Structured Artifact Tasks 5 and 9 | Node adapter binds validated input, resources, artifact paths/policy, activation/phase and requirement closure before key derivation; the Kernel accepts the frozen dispatch and fenced runtime context and resolves none of them again. |
| `AttemptNodeFactory.attempt` and `CapabilityBuildContext.attempt` | Attempt Task 10 / Foundation Task 8, extended by Checkpoint S | Existing graph-facing interface remains the only effectful node seam. Structured transport, materialization and finalization stay behind it and add zero graph nodes. |
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

The accepted design contains two statements that meet at the Intake `case-design` graph: it requires direct handling of all three legacy `min_matches` sites, and it also collapses prepare → Agent runtime → finalize into one semantic Attempt. The latter is the target authority. Therefore `case-design/prepare` and `case-design/repair-prepare` are characterized as fail-closed legacy fanouts, but are not recreated as LangGraph `Send` sites. Their two consumers become fixed Feature-owned prepare/finalize dataflow behind `ResolvedStructuredAgentExecutor`, with primary and repair tests proving finalize receives both the prepared value and validated Agent result. Only `generation/fanout` remains a target graph fanout and raises `InsufficientRouteMatches` before returning any `Send` values. This interpretation prevents phase plumbing from being reintroduced under a new name.

## Program Completion Definition

This program is complete only when all five workstreams—including all three Structured Artifact child plans—are checked off, their focused and repository gates pass, all live Invocations are revision-pinned to retained deployment artifacts, and the old Workflow Runtime is physically absent. “New graphs work while legacy remains indefinitely” is an intermediate migration state, not completion.
