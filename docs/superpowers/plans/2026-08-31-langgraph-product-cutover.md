# LangGraph Product Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compose the six Feature bundles into 14 revision-pinned Product `StateGraph` roots, move the `aa` lifecycle to `AssuranceApplication`, prove semantic parity through isolated shadow Invocations, cut over every entrypoint, drain legacy revisions, and physically delete Workflow YAML plus the custom Graph Runtime.

**Architecture:** `assurance-product` owns the fixed Feature factory allowlist, exact 14 public entrypoint contracts, Product-level state/adapters/routes, Product runtime bindings, local SQLite backend, and CLI assembly. Each Invocation is immutably marked `legacy-v2` or `langgraph-v1` and pinned to that runtime's authenticated build artifact: `InvocationLock` v2 for legacy or GraphRevision/ProductLock v3 for LangGraph. Temporary code-owned entrypoint switches control only new Invocation creation. After parity/cutover and a zero-active-legacy drain proof, deletion removes YAML assembly/compiler/projection/planner/token/scheduler authority; LangGraph remains the only Workflow engine.

**Tech Stack:** Python 3.11, LangGraph `1.2.11`, Async SQLite checkpointer `3.1.1`, Pydantic v2, Click, pytest, existing Product lock/source authentication/export/archive contracts and wheel smoke script.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, sections 8, 16–27, Product/revision/persistence tests, acceptance criteria, and explicit deletion target.

---

## Global Constraints

- Foundation, Attempt Kernel and all six Feature graph plans are complete and reviewed. Product Tasks 1–4 and T5a are also complete at continuation baseline `4a9cd197`. Execute remaining work in a clean continuation worktree; preserve the original dirty worktree and do not replay completed tasks.
- Every Product root state inherits `CheckpointBridgeState`; public input/output and semantic parity projections exclude `assurance_checkpoint_markers`, while restart tests retain checkpoint-integrity coverage for it.
- Remaining tasks execute in this order: OpenCode Task 0 / Checkpoint S0, Structured R1–R5 / Checkpoint S, T5b, T5c, T5d, then Tasks 6–10. A red S0 parks the Structured retrofit before production implementation; a green S0 only authorizes that implementation and does not authorize Agent cutover. Task 7 migrates every compiler/Runtime consumer and creates compatibility wrappers without deleting authority; Task 8 may then delete Assurance YAML/phase aliases; Task 9 atomically removes compiler and Runtime authority. Every intermediate commit must pass its stated gate.
- Keep public entrypoint names and public input/output/Status schemas stable. Internal graph/node/token IDs are not parity contracts.
- Product imports exactly six authenticated factory symbols and is the only cross-Feature graph composer. Boot remains generic: it consumes Product-provided owner-keyed factories/bundles and does not hard-code Assurance keyword arguments.
- Feature subgraphs are compiled with `checkpointer=None`. Product roots alone receive the anchored saver. Schema-different child invocation goes through a pure adapter; no side effect runs before an interrupting child call.
- One production Invocation has one immutable discriminated selection record: `LegacyRuntimeRecord(invocation_lock_digest, root_input_digest, entrypoint)` or `LangGraphRuntimeRecord(graph_revision_id, product_lock_digest, root_input_digest, entrypoint)`. Old/new runtimes may process different Invocations but never the same ID; a v2 lock digest is never relabeled as a GraphRevision.
- Shadow uses scripted/snapshotted data or separately named Invocations and cannot publish canonical SUT mutation twice. Compare semantic behavior, not private IDs.
- A new entrypoint switch affects only future starts. Existing Invocations always reopen their recorded runtime/revision. Rollback is a new-start routing change, never an in-place engine change.
- Agent contracts use provider-neutral `requires_structured_output`; Product maps it to `opencode_structured_output` only for an exact OpenCode release/adapter/provider/model/schema certification promoted by Checkpoint S. Checkpoint S0's minimal canary report is non-promotable and cannot satisfy Product. OpenCode implements this through its internal `StructuredOutput` tool and terminal `info.structured`, not provider-native `response_format`. Official v1.18.26 remains `message-roundtrip-red`, so the current program state is parked at S0; do not work around this with speculative Structured implementation, a private source fork, or a false capability flag.
- SQLite is local single-host only. A multi-worker deployment requires a separately qualified transactional anchored backend, not SQLite mounted on shared storage.
- The nine-site `join:any` migration has zero semantic waivers. A Feature current-trigger regression blocks every not-yet-cut reachable root plus T5b–T10, drain, and deletion; it does not roll back the four accepted T5a roots or switch any existing Invocation away from its recorded runtime/revision. Product's typed assessment-trigger optimization is accepted only after predecessor exclusion and no-late-reactivation are proven; falling back to the same typed inbox/cursor is an equivalent implementation, not an exception. Never embed a point-level legacy join, discard a late arrival, or substitute predecessor-map/last-write-wins state.
- Legacy deletion starts only after Checkpoint S and T5d are green, all 14 cutover records pass, no active legacy Invocation can resume, original revision artifacts have satisfied retention policy, and replacement crash/interrupt/export/archive tests are green. Legacy drain and old LangGraph revision retention are independent gates.

## Exact Product contract table

The sole metadata source is `assurance_product.graphs.revisions.ENTRYPOINT_CONTRACTS`:

| Public name | Target | Recursion limit |
|---|---|---:|
| `intake` | `IntakeGraphs.prepare` thin root | 2048 |
| `case` | `IntakeGraphs.case` thin root | 1024 |
| `full` | Product full composition | 8192 |
| `execute` | Product execution/quality/healing composition | 4096 |
| `archive` | `ImprovementGraphs.archive` thin root | 512 |
| `retro` | `ImprovementGraphs.retro` thin root | 2048 |
| `issue-review` | `QualityGraphs.issue_review` thin root | 512 |
| `issue-analyze` | `QualityGraphs.issue_analyze` thin root | 512 |
| `issue-reconcile` | `QualityGraphs.issue_reconcile` thin root | 512 |
| `improvement-review` | `ImprovementGraphs.review` thin root | 512 |
| `improvement-evaluate` | `ImprovementGraphs.evaluate` thin root | 512 |
| `improvement-export` | `ImprovementGraphs.export` thin root | 512 |
| `improvement-apply` | `ImprovementGraphs.apply` thin root | 1024 |
| `improvement-rollback` | `ImprovementGraphs.rollback` thin root | 512 |

Each contract names input/output/state model symbols, their canonical JSON-Schema digests, state schema version `1`, and recursion limit. Business loop ceilings remain explicit state fields/constants and are independently tested.

### Task 1: Define Product state, revision contracts, and 12 thin roots

**Files:**

- Create: `packages/products/assurance-product/assurance_product/graphs/__init__.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/state.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/revisions.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/entrypoints.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/factory.py`
- Create: `tests/product/test_stategraph_entrypoints.py`
- Create: `tests/product/test_graph_revision_contracts.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`

**Interfaces:** 14 `EntrypointGraphContract` values and `ThinEntrypointGraphs` containing exactly the 12 independently compiled thin roots. Final `ProductGraphs(entrypoints, contracts)` is constructed only in Task 2 after `execute/full` exist; no placeholder roots are introduced. Product factory accepts generic owner-keyed Feature bundles and immediately validates them into `ProductFeatureBundles` for the private typed compositor.

- [ ] **Step 1: Write exact key/metadata tests.**

```python
def test_thin_graphs_and_contracts_have_exact_keys(thin_graphs) -> None:
    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert set(thin_graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS) - {"full", "execute"}
    assert len(thin_graphs.entrypoints) == 12


def test_entrypoint_digest_changes_with_schema_or_limit_not_compiled_repr() -> None:
    assert digest(contract) != digest(replace(contract, recursion_limit=contract.recursion_limit + 1))
    assert "CompiledStateGraph" not in canonical_contract_projection(contract)
```

Assert the exact limit table above and state schema version `1` for every root.

- [ ] **Step 2: Test Product factory input closure.**

`build_thin_entrypoint_graphs(*, context, features: Mapping[str, object])` accepts exactly six owner IDs, rejects missing/extra/duplicate/mistyped bundles, converts them to `ProductFeatureBundles`, and builds only the 12 declared thin roots. Boot remains framework-neutral and does not know `intake=` or other Assurance fields.

- [ ] **Step 3: Test 12 thin roots.**

Each thin root validates public input, invokes only the declared Feature export through a pure adapter when schemas differ, publishes public output/terminal, and compiles with the root saver. It is not one giant dispatcher and does not inspect an `entrypoint` value inside state.

- [ ] **Step 4: Run RED.**

```bash
uv run pytest -q \
  tests/product/test_stategraph_entrypoints.py \
  tests/product/test_graph_revision_contracts.py
```

Expected: `ModuleNotFoundError: assurance_product.graphs`.

- [ ] **Step 5: Implement state/metadata/factories and verify dry/runtime parity.**

Use JSON-compatible state values and explicit receipt reducers. Compile all 12 thin roots once through dry `GraphBuildContext` and once with the runtime context; their contract/dry-build projections must match exactly while only the runtime roots carry the saver.

- [ ] **Step 6: Commit Product metadata/thin roots.**

```bash
uv run pytest -q tests/product/test_stategraph_entrypoints.py tests/product/test_graph_revision_contracts.py
git add \
  packages/products/assurance-product/assurance_product/models.py \
  packages/products/assurance-product/assurance_product/graphs/__init__.py \
  packages/products/assurance-product/assurance_product/graphs/state.py \
  packages/products/assurance-product/assurance_product/graphs/revisions.py \
  packages/products/assurance-product/assurance_product/graphs/entrypoints.py \
  packages/products/assurance-product/assurance_product/graphs/factory.py \
  tests/product/test_stategraph_entrypoints.py \
  tests/product/test_graph_revision_contracts.py
git commit -m "feat: define Product LangGraph entrypoints"
```

### Task 2: Compose `execute`/`full` and rewrite four Product `join:any` sites

**Files:**

- Create: `packages/products/assurance-product/assurance_product/graphs/routes.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/execute.py`
- Create: `packages/products/assurance-product/assurance_product/graphs/full.py`
- Modify: `packages/products/assurance-product/assurance_product/graphs/state.py`
- Modify: `packages/products/assurance-product/assurance_product/graphs/factory.py`
- Create: `tests/product/test_product_stategraph_flow.py`
- Create: `tests/product/test_product_join_any.py`
- Create: `tests/product/test_product_interrupts.py`
- Create: `tests/product/test_product_recursion_limit.py`
- Create: `tests/product/test_stategraph_parallelism.py`
- Modify: `tests/architecture/exclusive_route_inventory.py`
- Modify: `tests/architecture/test_exclusive_route_migration.py`
- Modify: `tests/architecture/loop_scc_inventory.py`
- Modify: `tests/architecture/test_loop_scc_migration.py`

**Interfaces:** explicit full/execute builders; failure/coverage flow-local inboxes; typed assessment trigger; Product coverage decision; parent/child adapters; entrypoint recursion configuration.

- [ ] **Step 1: Port black-box composition behavior first.**

Write scripted Feature-bundle tests proving `full` composes Intake → case/generation → execute → terminal/Retro/Improvement routes and `execute` composes Generation → Execution → Quality → Healing/repair/rerun/report. Port external expectations from current generation/quality/coverage/healing/report/terminal tests without asserting old node IDs.

Complete `build_product_graphs` by merging the 12 thin roots with `execute` and `full`, attach `ENTRYPOINT_CONTRACTS`, and assert exact set equality/length 14 here. A missing, duplicate or extra root fails before `ProductGraphs` is returned.

- [ ] **Step 2: Implement/test `failed-join` current-trigger semantics.**

Incoming sources are initial `execute` and rerun `run`; downstream issue analysis receives the exact trigger's rounds used/budget. Test first arrival, same-business-epoch late arrival, replay deduplication, reducer merge-order independence, repeated epochs, and dispatch cursor. Retain both arrivals; do not collapse into predecessor map or take arbitrary dict order.

- [ ] **Step 3: Implement/test `coverage-needed`.**

Incoming sources are `quality` and `quality-recheck`; downstream coverage repair receives the exact current trigger. Cover the same inbox/late/replay/epoch/cursor matrix and coverage-repair loop budget.

- [ ] **Step 4: Characterize and rewrite assessment exits.**

Prove both required facts for `assess-satisfied` and `assess-unsatisfied`: satisfied/unsatisfied are mutually exclusive for each assessment, and the `quality` versus `quality-recheck` predecessors cannot both reach the same exit or arrive late after that exit has routed. Characterization must show that an initial satisfied/unsatisfied result terminates without scheduling repair, while `quality-recheck` is reachable only after an earlier `repair_required` result and its satisfied/unsatisfied exit schedules no later recheck. Then use one typed `AssessmentTrigger(source, coverage_state, rounds, evidence)` set by the current assessment and route immediately to report/report-unsatisfied. Tests prove exact trigger identity, at-most-one arrival, and fail closed if a crafted update claims both outcomes. If either predecessor-exclusion or late-reactivation characterization fails, use the same inbox/cursor pattern; do not silently choose one.

All four Product rows are zero-waiver commit gates. A mismatch blocks every not-yet-cut root that can reach the row plus T5b–T10, shadow approval, drain, and deletion; the four accepted T5a roots and all existing Invocations retain their recorded runtime/revision. The inbox fallback is the specified equivalent implementation, not permission to weaken current-trigger behavior.

- [ ] **Step 5: Test Product coverage interrupt and parent restart.**

Coverage decision accepts only `approve | reject`. Multiple Feature/Product interrupts resume via interrupt-ID mapping. Scalar resume is rejected when more than one is pending. A schema-adapter node performs only deterministic input construction before invoking an interrupting child; restart cannot duplicate work.

- [ ] **Step 6: Test parallel reducers and recursion.**

Parallel Generation results/receipts merge order-independently with no concurrent scalar write. Every root uses its exact declared recursion limit. Deliberate `GraphRecursionError` normalizes to runtime failure; business budget exhaustion reaches a distinct declared terminal.

Complete the checked 50-site exclusive-route inventory for Product-owned routes. Every Product route computes its complete named-match mapping and calls `select_exclusive_route`; parameterized cases prove zero selects the exact recorded `otherwise` target and two matches fail with ambiguity. The inventory has no `pending` row before Task 8 deletes YAML characterization or Task 9 deletes the compiler.

Close the two Product-owned loop-SCC rows, `product-execute/failed-join` and `product-execute/coverage-needed`, in the exact seven-anchor inventory. Each row must point to its passing current-trigger matrix; acceptance compares exact anchors derived from assembled `CompiledGraph.sccs`, not merely a count of seven, while full membership is retained as failure diagnostics.

- [ ] **Step 7: Run RED, implement, and run Product flow suites.**

```bash
uv run pytest -q \
  tests/product/test_product_stategraph_flow.py \
  tests/product/test_product_join_any.py \
  tests/product/test_product_interrupts.py \
  tests/product/test_product_recursion_limit.py \
  tests/product/test_stategraph_parallelism.py \
  tests/architecture/test_exclusive_route_migration.py \
  tests/architecture/test_loop_scc_migration.py
```

Expected initially: missing `execute/full` builders; subsequent failures isolate inbox, interrupt and recursion behavior.

- [ ] **Step 8: Commit Product composition.**

```bash
git add \
  packages/products/assurance-product/assurance_product/graphs/state.py \
  packages/products/assurance-product/assurance_product/graphs/routes.py \
  packages/products/assurance-product/assurance_product/graphs/execute.py \
  packages/products/assurance-product/assurance_product/graphs/full.py \
  packages/products/assurance-product/assurance_product/graphs/factory.py \
  tests/product/test_product_stategraph_flow.py \
  tests/product/test_product_join_any.py \
  tests/product/test_product_interrupts.py \
  tests/product/test_product_recursion_limit.py \
  tests/product/test_stategraph_parallelism.py \
  tests/architecture/exclusive_route_inventory.py \
  tests/architecture/test_exclusive_route_migration.py \
  tests/architecture/loop_scc_inventory.py \
  tests/architecture/test_loop_scc_migration.py
git commit -m "feat: compose Product LangGraph workflows"
```

### Task 3: Assemble coexistence Application/CLI routing with legacy as the default

**Files:**

- Create: `packages/products/assurance-product/assurance_product/application.py`
- Create: `packages/products/assurance-product/assurance_product/runtime_selection.py`
- Create: `packages/products/assurance-product/assurance_product/revision_registry.py`
- Create: `packages/products/assurance-product/assurance_product/runtime_ports.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Modify: `packages/products/assurance-product/assurance_product/status.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Create: `tests/product/test_cli_langgraph_lifecycle.py`
- Create: `tests/product/test_application_export_archive.py`
- Create: `tests/product/test_runtime_selection_security.py`
- Create: `tests/product/test_product_runtime_ports.py`
- Create: `tests/product/test_cli_sqlite_system_interrupt.py`
- Modify: `tests/product/test_cli_compile.py`
- Modify: `tests/product/test_cli_lifecycle.py`
- Modify: `tests/product/test_cli_status_and_lock.py`
- Modify: `tests/product/test_cli_fail_closed.py`
- Modify: `tests/product/test_cli_export.py`
- Modify: `tests/product/test_cli_bindings_build.py`
- Modify: `tests/product/test_archive_after_publish.py`

**Interfaces:** `AssuranceProductApplication.compile/start/run/resume/status/export/archive/bindings_build/lock_show`; lifetime-managed `ProductRuntimePorts`; thin Click adapter; coexistence build bundle containing legacy `InvocationLock` v2 plus ProductLock v3/`GraphBuildManifest`; anchored SQLite; typed multiple-interrupt resume; code-owned all-legacy default; immutable runtime/revision record.

- [ ] **Step 1: Write compile contract tests.**

During coexistence, `aa compile` authenticates Product/six wheels once and emits a `CoexistenceBuildArtifacts` bundle: the byte-stable legacy `InvocationLock` v2/compiled Workflow artifact required by not-yet-cut-over new starts, plus ProductLock v3 and serializable `GraphBuildManifest` for LangGraph starts. ProductLock v3/manifest contain no compiled graph, `workflow_digest`, saver, Kernel, secret or workspace path; legacy Workflow fields exist only in the v2 member. Source/model/factory changes alter the v3 revision. A valid organization-config change alters ProductLock/revision but not topology; a SUT or `.aa/` topology attempt is rejected. After Task 6 proves zero active legacy Invocations, Task 8 atomically changes compile output to v3-only before deleting the YAML needed to construct the legacy member.

- [ ] **Step 2: Write start/run/restart tests.**

Create `ENTRYPOINT_RUNTIME_CUTOVER` here with all 14 values set to `legacy-v2`; no CLI/environment/config flag can override it. A test-only Product-owned selector may choose `langgraph-v1` to exercise the new path before cutover. New starts install a recoverable discriminated selection handshake at `.runtime/langgraph/selections/<invocation_id>.json` under a namespace lock. Phase 1 atomically writes an `initializing` `LegacyRuntimeRecord` or `LangGraphRuntimeRecord` containing runtime kind, immutable build identity, entrypoint and root-input digest. Phase 2 idempotently creates/verifies the legacy v2 identity or the anchored LangGraph initial checkpoint without running a business node. Phase 3 atomically replaces the record with `initialized` plus the authenticated lock/initial-checkpoint anchor digest. Only `initialized` is resumable. Reopen of `initializing` replays Phase 2/3 after verifying every byte; it never selects again. `run` holds one runner lease for LangGraph, invokes the pinned graph and maps six statuses. Close the process/SQLite connection and resume the same thread in a reconstructed Product Application.

For a pre-migration Invocation with no selection record, accept only `marker absent + authenticated legacy invocation.lock.json + valid legacy ledger/identity` as `legacy-v2`, then idempotently backfill an `initialized` canonical selection record. Reject absent/corrupt evidence, both-runtime artifacts, record/evidence disagreement and symlink/path collisions. Parameterize crashes after the initializing write, after backend identity creation, and before the initialized replacement; every restart completes the same selection/identity or fails closed on disagreement. Existing live legacy Invocations therefore remain routable without pretending they were originally marked.

Define `ProductRuntimePorts` as one async lifetime boundary constructed per CLI/Application operation. It opens the SQLite checkpoint/outbox/journal transaction backend, durable Attempt journal, resource-authorization store, workspace/activity/secret/effect adapters and authenticated registries; builds one `AssuranceAttemptKernel`; creates `AttemptCheckpointObserver` over that same Attempt journal; registers the observer in `AnchoredCheckpointer` before Boot compiles any root; and supplies the same Kernel/fence/revision ports through runtime context. Shutdown closes in reverse order only after observer/outbox recovery. A fake-only or empty production observer registry is forbidden.

`test_cli_sqlite_system_interrupt.py` drives a real Product graph/Kernel to pending, closes the process, reopens SQLite, resumes to commit, and asserts durable `SystemInterruptIssuanceAnchored` plus final-checkpoint `SystemInterruptCompletionCheckpointed` events and zero active generations. It also crashes after the completion pending write but before `aput` and proves the ordinal replays. The test goes through Click/Application assembly, not a manually wired saver fixture.

- [ ] **Step 3: Write typed resume/status tests.**

Add `--resume-file PATH` containing one human/system envelope or an interrupt-ID mapping. Keep `--action/--reason` only as single-human-interrupt sugar and make it mutually exclusive with the file. Reject unknown/missing/duplicate IDs and ambiguous scalar values. Status reads the anchored graph snapshot plus receipt refs; journal receipts enrich evidence but cannot advance Workflow status.

- [ ] **Step 4: Preserve export/archive/bindings/lock commands without the old driver.**

For a LangGraph Invocation, `export` and CLI `archive` read the revision-pinned anchored terminal snapshot plus authenticated receipt/evidence references; they never call legacy `Engine`, `driver` or `fold_events`. Existing legacy records keep their legacy adapter until drain. `bindings build` continues to use authenticated catalogs and `lock show` renders v2 for a legacy record or ProductLock v3/GraphRevision for a LangGraph record, failing on ambiguity. Add restart and tampered-receipt tests before any old driver deletion.

- [x] **Step 5: Install the historical fail-closed dispatch/cutover hook.**

Task 3 installed the Product-owned place where a reachable Agent requirement blocks before dispatch/cutover. Its original `provider_schema` interpretation is superseded by Checkpoint S and is not a second implementation task. The Structured Artifact tranche replaces that hook atomically with provider-neutral `requires_structured_output -> opencode_structured_output` resolution, exact release certification, terminal `info.structured` observation, and independent Kernel validation. Product Boot continues to compile/authenticate direct roots; only a selected reachable Agent dispatch/cutover fails closed while the capability is absent.

- [ ] **Step 6: Test CLI security and exit codes.**

No flag accepts graph path, Python import string, Workflow YAML or execution-contract override. SUT Python cannot load. Preserve source/project/change/invocation/input/secret flags. Map `completed=0`, nonterminal running/blocked/stopped to existing non-success lifecycle code, interrupted to the existing interrupt code, and failure/conflict/integrity to the existing failure code asserted by current CLI tests.

- [ ] **Step 7: Run RED, implement a thin coexistence CLI, and verify.**

```bash
uv run pytest -q \
  tests/product/test_cli_compile.py \
  tests/product/test_cli_lifecycle.py \
  tests/product/test_cli_status_and_lock.py \
  tests/product/test_cli_fail_closed.py \
  tests/product/test_cli_langgraph_lifecycle.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_runtime_selection_security.py \
  tests/product/test_product_runtime_ports.py \
  tests/product/test_cli_sqlite_system_interrupt.py \
  tests/product/test_cli_export.py \
  tests/product/test_cli_bindings_build.py \
  tests/product/test_archive_after_publish.py
```

Expected initially: compile has no coexistence bundle, selection/backfill records do not exist, and CLI directly drives `Engine/Ledger/fold_events`. After implementation, production selection still chooses legacy for all 14 names; only the explicit test-owned selector reaches LangGraph.

- [ ] **Step 8: Commit Product Application/CLI coexistence.**

```bash
git add \
  packages/products/assurance-product/assurance_product/application.py \
  packages/products/assurance-product/assurance_product/runtime_selection.py \
  packages/products/assurance-product/assurance_product/revision_registry.py \
  packages/products/assurance-product/assurance_product/runtime_ports.py \
  packages/products/assurance-product/assurance_product/cli.py \
  packages/products/assurance-product/assurance_product/status.py \
  packages/products/assurance-product/assurance_product/models.py \
  packages/products/assurance-product/assurance_product/product.py \
  tests/product/test_cli_compile.py \
  tests/product/test_cli_lifecycle.py \
  tests/product/test_cli_status_and_lock.py \
  tests/product/test_cli_fail_closed.py \
  tests/product/test_cli_langgraph_lifecycle.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_runtime_selection_security.py \
  tests/product/test_product_runtime_ports.py \
  tests/product/test_cli_sqlite_system_interrupt.py \
  tests/product/test_cli_export.py \
  tests/product/test_cli_bindings_build.py \
  tests/product/test_archive_after_publish.py
git commit -m "feat: add revision-pinned Product Application coexistence"
```

### Task 4: Build semantic shadow parity for all 14 entrypoints

**Files:**

- Create: `tests/product/shadow_harness.py`
- Create: `tests/product/test_langgraph_shadow_parity.py`
- Create: `tests/product/test_entrypoint_cutover.py`
- Create: `tests/product/validator_parity_fixture.py`
- Create: `tests/product/test_validator_shadow_parity.py`
- Modify: `tests/product/product_runner.py`
- Modify: `tests/product/composition_harness.py`
- Modify: `tests/product/conformance.py`
- Modify: `tests/product/test_binding_coverage.py`
- Modify: `tests/product/test_composition_authority.py`
- Modify: `tests/product/test_full_graph_audit.py`
- Modify: `tests/product/runtime_composition.py`

**Interfaces:** `SemanticTrace` and normalized comparators for Attempt calls, pure transforms, route decisions, ordered validator calls, interrupts, receipts, terminal/output; separate Invocation identities; per-entrypoint parity record; test-only cloned legacy/new contracts binding the same authenticated validator.

- [ ] **Step 1: Define the normalized trace.**

```python
@dataclass(frozen=True, slots=True)
class SemanticTrace:
    entrypoint: str
    attempts: tuple[SemanticAttemptCall, ...]
    pure_decisions: tuple[SemanticDecision, ...]
    validator_calls: tuple[SemanticValidatorCall, ...]
    interrupts: tuple[SemanticInterrupt, ...]
    receipts: tuple[SemanticReceipt, ...]
    terminal_status: str
    public_output: object
```

Map each legacy prepare/execute/finalize triple to one Agent contract call and map the four pure legacy handlers to pure decisions. Ignore alias IDs, activation/token IDs, checkpoint IDs, node names and internal graph count.

- [ ] **Step 2: Prove shadow isolation.**

Legacy and new traces use different Invocation IDs and isolated workspaces or a no-promotion scripted Kernel. A guard rejects an attempt to attach both drivers to one Invocation. No external effect handler is applied twice; effect traces are scripted or separately namespaced.

- [ ] **Step 3: Parameterize all public roots and meaningful branches.**

For all 14 names compare valid input/output/status plus failure and interrupt paths. For `execute/full`, cover generation family selection, execution failure/healing/rerun, coverage repair/human decision, report satisfied/unsatisfied, effect pending, and budget exhaustion. For looped joins compare downstream current-trigger inputs and repeat activation count. For standalone `improvement-evaluate` and the evaluate occurrence inside `improvement-apply`, compare the full effect kind `assurance.improvement.effect.delivery.v1`, payload discriminator `memory_eval`, and `MemoryEvalReceipt`. Standalone evaluate must cover committed, pending, and publication-indeterminate/recover traces; it cannot report success before effect settlement, and recovery cannot dispatch the evaluator twice.

- [ ] **Step 4: Prove one real test-only Validator binding on both runtimes.**

`validator_parity_fixture.py` resolves the authenticated `assurance.execution.validator.evidence.v1` registry entry and Boot-resolved core contract for `assurance.execution.agent.execute.v1` once. For the legacy adapter, clone one test-owned task definition with `ResourceClaims(writes=("tests", "src"))` and bind that validator. For LangGraph, clone the resolved contract's data-only `TaskAttemptContract` as `test.assurance.execution.validator-parity.v1`, give it the identical resources and validator tuple, then construct a test-only `ResolvedAttemptContract` with the existing authenticated Execution executor. Neither clone may create a new Agent runtime binding or enter a contribution, production catalog, ProductLock, GraphBuildManifest, wheel, or production inventory.

Run identical accepted and rejected candidates through the actual legacy commit path and actual LangGraph/Kernel path. `tests/test_validator_parity.py` and `src/validator_parity.py` must both pass resource/seal admission. The first calls the validator exactly once and promotes on both sides. The second calls it exactly once, returns the equivalent typed rejection, and performs zero durable commit-prepare (`workspace.prepare`/prepared journal event) plus zero canonical promotion on either side; Agent prepare/runtime/finalize needed to produce the sealed staged set may already have run. Assert afterward that all shipped contracts still declare `validators=()` and the authenticated production inventory remains exactly 25 registered / 0 bound.

- [ ] **Step 5: Run RED then implement trace adapters.**

```bash
uv run pytest -q \
  tests/product/test_langgraph_shadow_parity.py \
  tests/product/test_validator_shadow_parity.py \
  tests/product/test_entrypoint_cutover.py
```

Expected: missing shadow harness/parity records; mismatches are reported by semantic field, not raw event diff.

- [ ] **Step 6: Port/freeze existing Product black-box cases.**

Run current intake/triplet/generation/parallel/execution-quality/coverage/healing/interrupt/Retro/report/replay/terminal behavioral suites against both adapters where meaningful. Replace YAML structure expectations only after an equivalent behavior test exists.

- [ ] **Step 7: Commit shadow parity.**

```bash
git add \
  tests/product/shadow_harness.py \
  tests/product/test_langgraph_shadow_parity.py \
  tests/product/test_entrypoint_cutover.py \
  tests/product/validator_parity_fixture.py \
  tests/product/test_validator_shadow_parity.py \
  tests/product/product_runner.py \
  tests/product/composition_harness.py \
  tests/product/conformance.py \
  tests/product/test_binding_coverage.py \
  tests/product/test_composition_authority.py \
  tests/product/test_full_graph_audit.py \
  tests/product/runtime_composition.py
git commit -m "test: prove semantic LangGraph parity"
```

### Task 5: Cut over entrypoints without switching existing Invocations

**Continuation status:** Task 5 is partial. T5a is complete at `4a9cd197`; Checkpoint S0, Checkpoint S, and T5b–T5d are unexecuted. While S0 is red, only plan synchronization and OpenCode Task 0 are authorized.

**Files:**

- Modify: `packages/products/assurance-product/assurance_product/runtime_selection.py`
- Modify: `packages/products/assurance-product/assurance_product/revision_registry.py`
- Modify: `packages/products/assurance-product/assurance_product/application.py`
- Modify: `tests/product/test_entrypoint_cutover.py`
- Create: `tests/product/test_revision_retention.py`
- Modify: `tests/product/test_runtime_selection_security.py`

**Interfaces:** code-owned temporary `ENTRYPOINT_RUNTIME_CUTOVER`, immutable Invocation runtime marker, active-revision registry, rollback for future starts only.

- [x] **Step 1: Test immutable runtime/revision selection.**

First start records runtime kind/revision with the Task 3 `initializing → backend identity → initialized` recovery handshake. Reopening ignores the current switch even while initialization is incomplete and resumes only that recorded choice. Same Invocation ID cannot acquire both runtime directories/markers. Revision mismatch reports the required artifact before checkpoint read or Kernel call.

- [x] **Step 2: Test switch authority.**

Only Product code/release data can change new-start selection. `.aa/`, SUT, environment variables and CLI flags cannot select runtime/factory/revision. Switch keys equal the exact 14 public names; missing/extra values fail Boot.

- [ ] **Step 3: Execute the remaining reviewed tranches around Checkpoint S.**

The authoritative sequence is:

```text
T5a — complete at 4a9cd197: 4 non-Agent thin roots
  improvement-evaluate, improvement-export,
  improvement-apply, improvement-rollback

Checkpoint S0 — pending; current v1.18.26 candidate is red
  Run only the standalone exact-release OpenCode eligibility probe.
  Red: park Structured R1–R6 and all Product work below.
  Green: authorize Structured implementation, but no Product cutover.

Checkpoint S — pending
  Retrofit Structured Artifact transport/contracts/Kernel into the existing graphs,
  re-certify T5a and the pre-S/post-S revision fence,
  fully certify one exact OpenCode release and all 33 contracts on the final candidate.

T5b — pending: 8 Agent-dependent thin roots
  intake, case, archive, retro,
  issue-review, issue-analyze, issue-reconcile,
  improvement-review

T5c — pending: execute
T5d — pending: full
```

T5a is not a completed twelve-root Wave A. The four roots stay on LangGraph because their reachable Agent-contract set is empty. Checkpoint S0 blocks the start of Structured R1–R6; S0 or Checkpoint S being red blocks T5b–T5d and downstream drain/deletion. Neither condition rolls back T5a or globally fails direct/non-Agent Product Boot. After each pending cutover tranche, run its public behavior, restart, interrupt and export/archive prerequisites. Rollback changes only the switch for future starts; already-started LangGraph Invocations remain on their recorded revision.

Every T5b/T5c/T5d selector change creates a new candidate SHA. Before release, rerun the protected Checkpoint S aggregate gate for that exact candidate; external OpenCode qualification may be reused only when the authenticated adapter/server/provider/model/schema/limits and contract catalog closure are unchanged. The cutover record binds `candidate_sha`, `graph_revision_id`, `product_lock_digest`, `structured_output_certification_digest`, `binding_wheel_digest`, `attempt_site_catalog_digest`, entrypoint/runtime, and passing parity/crash/join citations from that same candidate. A missing, failed, xfailed, waived, cross-candidate, or drifted row blocks the tranche. T5a's post-S regression record must retain the cross-runtime test-only Validator evidence and standalone/apply `evaluate-memory-improvement` committed/pending/publication-indeterminate evidence; no production contract gains a Validator binding to satisfy it.

- [ ] **Step 4: Extend active revision retention across the pre-S/post-S fence.**

Structured source/schema/binding closure changes the authenticated wheel sources/ProductLock and therefore produces a distinct post-S GraphRevision; the post-S `GraphBuildManifest` must authenticate the structured Attempt digests. Existing pre-S Invocations retain their original selection, revision, lock, checkpoint, journal and receipt and resume only through the original deployment artifact/container. Missing artifact fails before checkpoint or Kernel access; relabel/backfill is forbidden. New starts use the post-S revision, and one process never imports two versions of the same wheel.

The current conservative binding-file counts protect artifacts from premature removal but do not prove an Invocation is terminal. Task 6 adds the authenticated resumable/terminal scan required for retirement and drain authorization.

- [ ] **Step 5: Verify and commit each remaining tranche.**

For each remaining tranche run:

```bash
uv run pytest -q \
  tests/product/test_langgraph_shadow_parity.py \
  tests/product/test_validator_shadow_parity.py \
  tests/product/test_entrypoint_cutover.py \
  tests/product/test_revision_retention.py \
  tests/product/test_runtime_selection_security.py
```

Use one explicit commit per remaining tranche:

```text
feat: cut over structured thin Product entrypoints
feat: cut over Product execute
feat: cut over Product full
```

For each remaining tranche, stage only the following code-owned selector/registry/application and cutover evidence files, then inspect the staged diff before using the matching commit message:

```bash
git add \
  packages/products/assurance-product/assurance_product/runtime_selection.py \
  packages/products/assurance-product/assurance_product/revision_registry.py \
  packages/products/assurance-product/assurance_product/application.py \
  tests/product/test_entrypoint_cutover.py \
  tests/product/test_revision_retention.py \
  tests/product/test_runtime_selection_security.py \
  tests/product/test_langgraph_shadow_parity.py
git diff --cached --name-only
```

### Task 6: Stop legacy starts and prove the drain gate

**Dependency:** Checkpoint S and T5b/T5c/T5d are green, and the authenticated selector is already `14/14 langgraph-v1` for future starts. Task 6 must not begin from T5a partial state.

**Files:**

- Modify: `packages/products/assurance-product/assurance_product/runtime_selection.py`
- Modify: `packages/products/assurance-product/assurance_product/revision_registry.py`
- Create: `packages/framework/graph-engine/graph_engine/evidence/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/evidence/legacy_v2.py`
- Create: `packages/framework/graph-engine/tests/evidence/test_legacy_v2_reader.py`
- Create: `tests/product/test_historical_v2_export_archive.py`
- Create: `tests/product/test_legacy_drain_gate.py`
- Modify: `tests/product/test_revision_retention.py`

**Interfaces:** no-new-legacy-start state, authenticated active-Invocation scan, explicit operator terminal record, deletion authorization.

- [ ] **Step 1: Write a fail-closed drain test.**

Deletion authorization fails when any legacy Invocation is running, blocked, interrupted, stopped-but-resumable, publication-indeterminate, or has unreadable identity. It succeeds only at zero active legacy, with every nonterminal old Invocation explicitly resumed to terminal or terminated by an authenticated operator record.

Authorization also fails if any of the exact nine `join:any` rows is missing, failed, xfailed, or carries a semantic waiver; if the exact seven loop-SCC anchor inventory no longer matches; if any of the three `min_matches` sites is implemented outside the frozen one-`Send`/two-Composite mapping; or if cross-runtime test-only Validator accept/reject parity is absent. These are migration evidence gates even when active legacy count is zero.

- [ ] **Step 2: Freeze the already-complete 14/14 new-start cutover.**

Verify all 14 switches are already `langgraph-v1`, remove rollback-to-legacy for new starts, and retain only legacy reopen/resume. Test a legacy-marked Invocation still opens with its old artifact while a new same-entrypoint Invocation always records LangGraph. Do not retire a pre-S LangGraph artifact merely because legacy active count is zero; its own authenticated resumable scan is independent.

- [ ] **Step 3: Run full crash/export/archive coverage before authorization.**

New runtime must cover promotion-before-checkpoint, effect unknown/reconcile, system interrupt replay, runner lease loss, SQLite restart, revision mismatch, human multiple interrupts, export and archive. Drain does not excuse a missing replacement test.

Before any Workflow declaration/compiler/runtime deletion, extract the minimum read-only evidence surface to `graph_engine.evidence.legacy_v2`: byte-exact `InvocationLock` v2 authentication plus legacy ledger/event integrity/fold needed by historical `export`, `archive`, and `lock show`. It exposes no append, resume, next-node, planner, scheduler or effect-settlement API. Golden/tamper tests and `test_historical_v2_export_archive.py` must pass while the original implementation is still available for differential comparison. Tasks 7–9 may delete their source modules only after this reader is green.

- [ ] **Step 4: Record zero-active evidence and commit.**

```bash
uv run pytest -q \
  tests/product/test_legacy_drain_gate.py \
  tests/product/test_revision_retention.py \
  tests/product/test_validator_shadow_parity.py \
  tests/product/test_publish_recovery.py \
  tests/product/test_archive_after_publish.py \
  tests/architecture/test_loop_scc_migration.py \
  packages/framework/graph-engine/tests/evidence/test_legacy_v2_reader.py \
  tests/product/test_historical_v2_export_archive.py
git add \
  packages/framework/graph-engine/graph_engine/evidence/__init__.py \
  packages/framework/graph-engine/graph_engine/evidence/legacy_v2.py \
  packages/framework/graph-engine/tests/evidence/test_legacy_v2_reader.py \
  packages/products/assurance-product/assurance_product/runtime_selection.py \
  packages/products/assurance-product/assurance_product/revision_registry.py \
  tests/product/test_legacy_drain_gate.py \
  tests/product/test_revision_retention.py \
  tests/product/test_historical_v2_export_archive.py
git commit -m "chore: close legacy Invocation creation"
```

### Task 7: Prepare one atomic compiler/Runtime authority deletion

**Dependency and execution position:** Execute this section immediately after Task 6 and before Task 8. It deletes no compiler/Runtime authority, so legacy historical paths remain usable for the subsequent YAML/alias cleanup commit.

**Files:**

- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/module_schema.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/compiler.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/expressions.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/input_projection.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/output_projection.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/schema.py`
- Retain unchanged until Task 9: `packages/framework/graph-engine/graph_engine/graph/__init__.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/declarative.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/resolver.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify only to add/use the coexistence Python branch; retain every legacy field/branch until Task 9: `packages/framework/graph-engine/graph_engine/composition/__init__.py`
- Retain until Task 9: `packages/framework/graph-engine/tests/composition/test_workflow_assembler.py`
- Retain until Task 9: `packages/framework/graph-engine/tests/graph/test_input_projection.py`
- Retain until Task 9: `packages/framework/graph-engine/tests/graph/test_output_projection.py`
- Retain until Task 9: `packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py`
- Retain until Task 9: `packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py`
- Create: `tests/architecture/test_legacy_workflow_deleted.py`
- Create: `tests/architecture/legacy_import_inventory.py`
- Create: `tests/architecture/test_legacy_import_inventory.py`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/product.py`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/plugin.py`
- Create: `examples/graph-engine-toy-a/graph_engine_toy_a/contracts.py`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/product-declaration.json`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/plugin-declaration.json`
- Modify: `examples/graph-engine-toy-a/pyproject.toml`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/product.py`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/plugin.py`
- Create: `examples/graph-engine-toy-b/graph_engine_toy_b/contracts.py`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/product-declaration.json`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/plugin-declaration.json`
- Modify: `examples/graph-engine-toy-b/pyproject.toml`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/product.py`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/plugin.py`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/contracts.py`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/product-declaration.json`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/plugin-declaration.json`
- Modify: `examples/agent-runtime-fixture/pyproject.toml`
- Modify: `packages/framework/graph-engine/tests/integration/test_toy_a.py`
- Modify: `packages/framework/graph-engine/tests/integration/test_toy_b.py`
- Modify: `tests/product/test_phase3_live_fixture_contract.py`
- Modify: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product.py`
- Modify: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-cursor-declaration.json`
- Modify: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-opencode-declaration.json`
- Delete: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/workflow.yaml`
- Modify: `tests/phase4/test_six_wheel_composition.py`
- Modify: `tests/phase4/fixtures/six-wheel-product/pyproject.toml`
- Modify/regenerate: `uv.lock`
- Modify: `packages/framework/graph-engine/tests/composition/test_declarative_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_manifest_projection.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_wheel_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registry_platform.py`
- Modify: `tests/product/test_composition_authority.py`
- Modify: `tests/product/test_full_graph_audit.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/activity.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/workspace.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/production_host.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/production_worker.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/host_protocol.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/host_receipts.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/secret_sources.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/activity.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/production_host.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/production_worker.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/host_protocol.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/host_receipts.py`
- Modify as temporary re-export wrappers: `packages/framework/graph-engine/graph_engine/runtime/secret_sources.py`
- Modify: `packages/framework/graph-engine/graph_engine/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/__main__.py`
- Modify: `benchmark/agent-runtime-phase3/run_item.py`
- Modify: `scripts/graph_engine_smoke_test.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/products/assurance-product/assurance_product/runtime_ports.py`
- Modify: `tests/product/test_product_runtime_ports.py`
- Retain as an explicitly allowlisted temporary legacy execution adapter until Task 9: `packages/products/assurance-product/assurance_product/application.py`
- Retain as an explicitly allowlisted temporary legacy execution adapter until Task 9: `packages/products/assurance-product/assurance_product/cli.py`
- Retain as an explicitly allowlisted temporary legacy execution adapter until Task 9: `packages/products/assurance-product/assurance_product/status.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/context.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/resource_arbiter.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py`
- Modify every exact path in the “Task 7 retained-consumer migration set” below; those paths are part of Task 7 even though grouped beside the Task 9 precondition.

**Interfaces:** migrated generic Python Product factory examples whose effectful nodes use authenticated `TaskAttemptContract`s; new `graph_engine.attempts` homes for retained activity/host/workspace/security primitives; Boot/Application CLI and smoke path; checked zero-external-consumer inventory. The legacy compiler and Runtime remain behaviorally executable through retained legacy composition branches for this compatibility commit and are removed together in Task 9.

- [ ] **Step 1: Add the future negative import/symbol tests without deleting authority yet.**

Make the negative scanner enumerate `WorkflowModuleDef`, `GraphDef`, `NodeDef`, `compile_workflow`, `assemble_product_workflow`, expression/input/output projection modules and Runtime authority. During this Task it may report only (a) the explicitly retained compiler/Runtime implementation and compatibility wrappers, including the exact Product `application.py`/`cli.py`/`status.py` legacy execution adapters, (b) every exact production or test consumer with a `Modify`, `Delete`, or `Delete/replace` disposition in Task 8, and (c) compiler/Runtime characterization tests and adapters explicitly modified/deleted in Task 9. The allowlist is generated from those explicit inventories, never from a directory or wildcard, so it cannot grow implicitly. Task 8 must drain every one of its rows; Task 9 removes the remaining implementation/wrapper/Product-adapter/test rows and requires an empty scan.

- [ ] **Step 2: Finish Product/example conversion without mutating retained v3 canonical data.**

ProductLock v3/new paths contain no Workflow/module/slot fields or compiled Workflow digest. Do not delete or reinterpret the legacy `ProductManifest`, assembler, resolver or lock branches in this Task: existing legacy tests must still pass unchanged. Legacy `InvocationLock` v2 remains until the atomic Task 9 deletion; historical access already uses the Task 6 evidence reader.

- [ ] **Step 3: Migrate every compiler/Runtime consumer while compatibility wrappers still exist.**

Copy the retained activity/workspace/host/security implementations into `graph_engine.attempts`; the repository already provides the reusable top-level JSON-Schema utility and its temporary `runtime/json_schema.py` re-export, and Semantic Attempt Task 9 has already moved both effect engines to the retained import. Migrate `runtime_ports.py`, its test, and all retained tests/helpers in the exact Task 9 consumer set to the new imports. Rewrite `graph_engine.__main__`, the generic smoke script and benchmark driver to Boot/Application/Attempt APIs while the old Product execution adapter remains available only to already drained compatibility tests. Keep PyYAML if closed Product/config declarations still need it.

Convert both `graph-engine-toy-a`/`toy-b` and `agent-runtime-fixture` from embedded `WorkflowDef`/Workflow declarations to explicit Product-supplied Python `StateGraph` factories built through the generic Boot/Application contracts. Move each toy's file-writing/retrying handler policy into authenticated owner contracts in its new `contracts.py`, publish those from `plugin.py`, and call them only through `CapabilityBuildContext.attempt`; direct file mutation from a graph node is forbidden. Preserve their integration role: the two toys still prove graph-engine is a reusable Spring-Boot-like framework rather than Assurance-only code, and the Agent fixture still exercises adapter rebinding through a composite Attempt. Convert the Phase 4 six-wheel fixture/declarations and delete only its test-owned Workflow YAML after its StateGraph replacement passes. Update the import inventory; do not remove `graph.schema` in this Task.

Pin `langgraph==1.2.11` directly in all three example `pyproject.toml` files and the Phase 4 six-wheel fixture, then regenerate `uv.lock`. Migrate the four composition tests, Product composition-authority test and full-graph audit listed in `Files` to the Python Product/Application surface before compatibility modules disappear.

- [ ] **Step 4: Verify and commit.**

```bash
uv run pytest -q tests/architecture/test_legacy_workflow_deleted.py \
  tests/architecture/test_legacy_import_inventory.py \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence \
  packages/framework/graph-engine/tests/runtime \
  packages/framework/graph-engine/tests/composition \
  packages/framework/graph-engine/tests/boot \
  packages/framework/graph-engine/tests/integration \
  tests/product/test_phase3_live_fixture_contract.py \
  tests/phase4/test_six_wheel_composition.py
uv run lint-imports
uv run pyright
```

Stage every modified/created/deleted Task 7 `Files` path except the lines explicitly marked “Retain unchanged until Task 9”; include the three example Products/declarations, both toy integration tests, Phase 4 fixture conversion/deletion, seven new Attempt homes and wrappers, Product `runtime_ports.py`/its test, CLI/smoke/benchmark/CI, retained-consumer imports and inventory updates. Inspect `git diff --cached --name-status`, then:

```bash
git commit -m "refactor: prepare atomic Workflow Runtime deletion"
```

### Task 8: Atomically switch compile to v3-only and delete Workflow YAML, module packaging, and phase aliases

**Dependency:** Task 6 has authenticated zero active legacy Invocations, and Task 7 has committed the Python Product/example/consumer migration with the legacy import inventory clean outside explicitly retained implementation/compatibility modules. Do not execute Task 8 unless both gates are green. The v3-only compile transition and removal of the YAML inputs needed by v2 compilation are one commit; neither half may land alone.

**Files:**

- Delete: `packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Delete: `packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Delete: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Delete: `packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml`
- Delete: `packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml`
- Delete: `packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml`
- Delete: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Delete: `packages/products/assurance-product/assurance_product/resources/graph-inventory.yaml`
- Delete: `tests/product/fixtures/workflow-module-ownership.yaml`
- Delete as an unreferenced legacy compiler fixture: `tests/fixtures/workflow-v2-minimal.yaml`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_kit.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_plugin_contracts.py`
- Delete: `packages/capabilities/assurance-intake/assurance_intake/contracts/workflow.py`
- Delete: `packages/capabilities/assurance-generation/assurance_generation/contracts/workflow.py`
- Delete: `packages/capabilities/assurance-execution/assurance_execution/contracts/workflow.py`
- Delete: `packages/capabilities/assurance-quality/assurance_quality/contracts/workflow.py`
- Delete: `packages/capabilities/assurance-healing/assurance_healing/contracts/workflow.py`
- Delete: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Modify/regenerate: `packages/products/assurance-product/assurance_product/product-declaration-opencode.json`
- Modify/regenerate: `packages/products/assurance-product/assurance_product/product-declaration-cursor.json`
- Modify: `packages/products/assurance-product/assurance_product/application.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Modify: `tests/product/test_cli_compile.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_schema.py`
- Modify: `packages/products/assurance-product/assurance_product/agent_contracts.py`
- Retain/verify against the redefined 33-contract mapping: `packages/products/assurance-product/assurance_product/opencode_agents.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/output_routes.py`
- Modify: `packages/products/assurance-product/assurance_product/binding_builder.py`
- Delete/replace: `packages/capabilities/assurance-intake/tests/test_workflow_module.py`
- Delete/replace: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`
- Delete/replace: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Delete/replace: `packages/capabilities/assurance-quality/tests/test_workflow_module.py`
- Delete/replace: `packages/capabilities/assurance-healing/tests/test_workflow_module.py`
- Delete/replace: `packages/capabilities/assurance-improvement/tests/test_workflow_module.py`
- Delete: `tests/product/graph_inventory.py`
- Delete: `tests/product/test_workflow_modularization_golden.py`
- Delete: `tests/product/test_graph_binding_audit.py`
- Delete: `tests/product/test_workflow_module_security.py`
- Create: `tests/product/test_python_native_cutover.py`
- Modify: `tests/architecture/legacy_import_inventory.py`
- Modify: `tests/architecture/test_legacy_import_inventory.py`
- Modify: `tests/product/test_product_composition.py`
- Modify: `tests/product/test_product_providers.py`
- Modify: `tests/product/test_no_whole_tree_residuals.py`
- Modify: `tests/product/test_opencode_staging_boundary.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-execution/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_coverage.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_issues.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/review.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/contracts/decisions.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/__init__.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/decisions.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/__init__.py`
- Modify: `tests/product/test_agent_execution_contracts.py`
- Modify: `tests/product/test_binding_builder.py`
- Modify: `tests/product/test_binding_builder_security.py`
- Modify: `tests/product/test_change_local_output_routing.py`
- Modify: `tests/product/test_graph_intake_and_triplets.py`
- Modify: `tests/product/test_product_entrypoints.py`
- Modify: `tests/product/test_change_runtime_layout.py`
- Modify: `tests/product/test_cli_bindings_build.py`
- Modify: `tests/product/test_phase5_benchmark_change_layout.py`
- Modify: `tests/product/test_phase5_benchmark_manifest.py`
- Modify: `tests/product/test_phase5_final_fault_gate.py`
- Modify: `tests/product/test_phase5_ledgers.py`
- Modify: `benchmark/assurance-product/manifest.json`
- Modify: `tests/product/product_runner.py`
- Modify: `tests/product/composition_harness.py`
- Modify: `tests/product/conformance.py`
- Modify: `tests/product/test_binding_coverage.py`
- Modify: `tests/product/test_composition_authority.py`
- Modify: `tests/product/test_full_graph_audit.py`

**Interfaces:** Python factories are the only topology source; 33 Agent contracts/34 occurrences, eight direct Attempt contracts/nine occurrences, four pure functions/16 occurrences; zero aliases/phase slots/YAML resources.

Checkpoint S's 33 `AgentExecutionContract[InputT, PreparedT, AgentResultT, OutputT]` values, `ArtifactContract`/slot registries, result/document schemas, structured runtime binding identities, serializers, materialization receipts, and Attempt-site catalog are permanent Python-native authority. Task 8 removes only legacy YAML/module/phase-alias representations and their consumers; it must not redefine, flatten, or delete the Structured Artifact closure or restore composite/provider-schema fields.

**Dependency:** Foundation Task 3's coexistence `ProductManifest.graph_factory_symbol` branch is already green. Switch Assurance declarations to that branch here. Task 7 has already migrated the generic examples; only the framework's explicitly allowlisted legacy composition/compiler implementation remains until atomic Task 9.

- [ ] **Step 1: Add the deletion test while it is red.**

```python
def test_production_wheels_have_no_workflow_topology_resources(wheel_contents) -> None:
    forbidden = ("resources/workflow/module.yaml", "resources/workflow/main.yaml", "graph-inventory.yaml")
    assert not any(any(name.endswith(item) for item in forbidden) for name in wheel_contents)


def test_semantic_agent_nodes_have_no_phase_aliases(boot_artifact) -> None:
    assert len(boot_artifact.attempt_contracts) == 41
    assert count_agent_occurrences() == 34
    assert not registered_ids_with_prefix("assurance.product.agent.")


def test_compile_emits_only_v3_product_artifacts(compiled_artifacts) -> None:
    assert isinstance(compiled_artifacts.product_lock, ProductLockV3)
    assert isinstance(compiled_artifacts.graph_manifest, GraphBuildManifest)
    assert not hasattr(compiled_artifacts, "legacy_invocation_lock")
```

The same red suite names both test goldens, and the production-symbol inventory names `_WORKFLOW_MODULE_MIME`, `application/vnd.graph-engine.workflow-module+yaml`, and the `workflow/module.yaml` classifier in `plugin_kit.py`. Expected RED: eight production YAML/inventory files, two obsolete test fixtures, the Workflow-module MIME branch, 99 aliases/102 phase slots, and the coexistence compile member.

- [ ] **Step 2: Remove resources and declaration authority.**

After rechecking Task 6's zero-active authorization in this commit, change `aa compile`/Application to emit only ProductLock v3 plus `GraphBuildManifest` and remove `CoexistenceBuildArtifacts`/the v2 compile branch. Historical v2 `lock show`/export/archive continue through the read-only Task 6 evidence reader; no command may reconstruct an executable v2 Workflow. Then delete the eight production topology/inventory files and both obsolete test fixtures, remove their six plugin resource entries and Product Workflow manifest fields, and regenerate Product declarations without `workflow_module`, `workflow_module_resources` or `workflow_slot_bindings`. Remove the Workflow-specific MIME constant/path branch from `plugin_kit.py`; generic JSON/schema/closed-data resource classification remains. Keep closed organization YAML and PyYAML where still used.

- [ ] **Step 3: Remove phase plumbing.**

Delete `expand_agent_job_slots` from `agent_runtime_contracts/execution_contract.py`, `AGENT_SLOT_PHASES`, `PREPARE_IDS`, Product slot requirements/bindings, `bind_agent_execution_contracts`, alias helpers/audits and exact-99 closure. Move `output_routes.py` imports to surviving Attempt/domain contracts. Delete old `contracts/workflow.py` only after domain exports live in `contracts/attempts.py`, `contracts/decisions.py` or dedicated domain modules and imports are updated.

Move `AGENT_JOB_CONTRACTS` consumers to the six owner `contracts/attempts.py` catalogs and `OUTPUT_ROUTE_TEMPLATES` consumers to owner graph/domain route contracts. Create Quality `contracts/decisions.py` for `CoverageAssessmentPublicV1`, `IssueAnalysisPublicV1` and their decision-only closed vocabularies, reusing `CoverageState` from `contracts/coverage.py` rather than duplicating it. Create Improvement `contracts/decisions.py` for `APPLY_HUMAN_ACTIONS`, `AUTO_REVIEW_DECISIONS`, and `APPLY_EVALUATION_OUTCOMES`. Export both modules through their package `contracts/__init__.py`. Update the six `test_contracts.py`, Quality coverage/issues tests, Improvement review operation, and Product Agent-contract test listed in `Files`; no deleted `contracts.workflow` import may survive the Task 8 scanner.

Retain Product `AGENT_EXECUTION_CONTRACTS` as the exact 33 semantic-Agent mapping so `opencode_agents.py` keeps validating every profile reference without learning phase aliases. `test_opencode_staging_boundary.py` proves exact profile closure and absence of prepare/finalize alias IDs.

Rewrite `binding_builder.py` from phase `PREPARE_IDS`/99 aliases to an exact 33-record semantic runtime-binding manifest keyed by Agent contract ID while preserving `aa bindings build`. The builder still authenticates each contract's prepare/finalize handlers from `AgentExecutionContract`, but it does not emit them as independently selectable phase bindings. Migrate `benchmark/assurance-product/manifest.json` from 33 legacy prepare-ID route keys to the 33 semantic contract IDs and update the checked benchmark/Phase 5 tests. Remove `expand_agent_job_slots` from both implementation and `agent_runtime_contracts.__init__`; update model/schema public-export tests, builder/security/output-routing, CLI bindings, runtime layout and Phase 5 manifest/ledger/fault tests listed in `Files`. Before deleting `tests/product/graph_inventory.py`, migrate `test_graph_intake_and_triplets.py` and `test_product_entrypoints.py` to Python bundle/entrypoint contracts plus the checked route inventory.

Because Task 7 already moved Product helpers off the legacy assembler, finish the alias cut here: remove the drained legacy binding branch from `product_runner.py`/`composition_harness.py`; replace live `PREPARE_IDS`/`ALL_BINDING_IDS` in `tests/product/conformance.py` with exact semantic contract/runtime-binding IDs while keeping any old Phase 5 ledger identifiers in a clearly historical fixture; replace `test_binding_coverage.py` with exact 33 semantic-binding coverage; and migrate composition/full-graph audits to the 14 Python roots before deleting graph inventory.

- [ ] **Step 4: Replace, then delete, YAML structure tests.**

Keep behavior tests now backed by StateGraphs. Delete tests that only parse module shape, inventory or slot ownership, including their shared `workflow-module-ownership.yaml` golden; delete the already-unreferenced `workflow-v2-minimal.yaml` rather than silently blessing it as historical evidence. Historical v2 evidence tests use byte fixtures owned by `graph_engine.evidence.legacy_v2`, not executable topology YAML. Run all Capability/Product behavior tests before committing.

Use Task 7's checked AST/import inventory across `packages/`, `tests/`, `examples/`, `benchmark/`, and `scripts/`. Remove every Task 8 resource/alias consumer row as its replacement lands, then assert the only remaining allowlisted rows are the implementation/wrappers and characterization tests deleted by Task 9. Any unlisted consumer or scanner read/parse failure blocks the commit; a broad full-suite run is not a substitute for this proof.

Rewrite `test_product_composition.py` and `test_product_providers.py` in this Task: remove assertions for `workflow_module_resources`, `workflow_slot_bindings`, and the exact 6/99 legacy values; replace them with six factory refs, 41 Attempt contracts, 33 runtime bindings and 14 root contracts. Update `test_no_whole_tree_residuals.py` toy manifests to ProductLock v3/graph factories before deleting Product manifest fields.

- [ ] **Step 5: Verify and commit resource/alias deletion.**

```bash
uv run pytest -q tests/product/test_python_native_cutover.py
uv run pytest -q \
  tests/product/test_cli_compile.py \
  tests/product/test_product_composition.py \
  tests/product/test_product_providers.py \
  tests/product/test_no_whole_tree_residuals.py \
  tests/product/test_opencode_staging_boundary.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_binding_builder.py \
  tests/product/test_binding_builder_security.py \
  tests/product/test_change_local_output_routing.py \
  tests/product/test_graph_intake_and_triplets.py \
  tests/product/test_product_entrypoints.py \
  tests/product/test_change_runtime_layout.py \
  tests/product/test_cli_bindings_build.py \
  tests/product/test_phase5_benchmark_change_layout.py \
  tests/product/test_phase5_benchmark_manifest.py \
  tests/product/test_phase5_final_fault_gate.py \
  tests/product/test_phase5_ledgers.py \
  tests/product/product_runner.py \
  tests/product/composition_harness.py \
  tests/product/conformance.py \
  tests/product/test_binding_coverage.py \
  tests/product/test_composition_authority.py \
  tests/product/test_full_graph_audit.py \
  tests/architecture/test_legacy_import_inventory.py
uv run pytest -q packages/framework/graph-engine/tests/composition/test_plugin_contracts.py
uv run pytest -q tests/product
uv run pytest -q packages/capabilities/assurance-intake/tests \
  packages/capabilities/assurance-generation/tests \
  packages/capabilities/assurance-execution/tests \
  packages/capabilities/assurance-quality/tests \
  packages/capabilities/assurance-healing/tests \
  packages/capabilities/assurance-improvement/tests
uv run lint-imports
bash scripts/assurance_capability_wheel_smoke_test.sh
```

Stage the exact `Files` paths above (including eight production resource deletions, both test-fixture deletions, the framework MIME cleanup, v3-only Application/CLI compile transition, six contract-module deletions, both new decision modules/exports, six plugin/declaration pairs, Product declarations/models/tests, and the two inventory files) with explicit `git add` arguments; inspect `git diff --cached --name-status`, then:

```bash
git commit -m "refactor: remove Workflow YAML and phase aliases"
```

### Task 9: Atomically delete the Workflow compiler and custom Runtime

**Files:**

- Delete: `packages/framework/graph-engine/graph_engine/graph/module_schema.py`
- Delete: `packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/compiler.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/expressions.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/input_projection.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/output_projection.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/schema.py`
- Delete: `packages/framework/graph-engine/graph_engine/graph/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/declarative.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/resolver.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/__init__.py`
- Delete: `packages/framework/graph-engine/tests/composition/test_workflow_assembler.py`
- Delete: `packages/framework/graph-engine/tests/graph/test_input_projection.py`
- Delete: `packages/framework/graph-engine/tests/graph/test_output_projection.py`
- Delete: `packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py`
- Delete: `packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_declarative_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_manifest_projection.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registry_platform.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_wheel_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`
- Modify: `tests/product/test_composition_authority.py`
- Modify: `tests/product/test_full_graph_audit.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/planner.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/scheduler.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/engine.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/driver.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/checkpoint.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/seed.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/models.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/events.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/ledger.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/effects.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/invocation_lock.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/frozen_json.py`
- Delete: `packages/framework/graph-engine/graph_engine/runtime/json_schema.py`
- Retain/modify imports only: `packages/framework/graph-engine/graph_engine/evidence/__init__.py`
- Retain/modify imports only: `packages/framework/graph-engine/graph_engine/evidence/legacy_v2.py`
- Retain: `packages/framework/graph-engine/tests/evidence/test_legacy_v2_reader.py`
- Retain: `tests/product/test_historical_v2_export_archive.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/activity.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/production_host.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/production_worker.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/host_protocol.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/host_receipts.py`
- Delete Task 7 compatibility wrapper: `packages/framework/graph-engine/graph_engine/runtime/secret_sources.py`
- Delete after all moves/import rewrites: `packages/framework/graph-engine/graph_engine/runtime/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/__main__.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_driver.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_effects.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_engine.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_invocation_lock.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_invocation_seed.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_planner.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_planner_fatal_barrier.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_planner_input_projection.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_planner_routing.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_scheduler.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_scheduler_faults.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_scheduler_workspace_identity.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_structural_failure_fold.py`
- Delete: `packages/framework/graph-engine/tests/runtime/bootstrap_fixtures.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_bootstrap_v2.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_cli_host.py`
- Delete: `packages/framework/graph-engine/tests/runtime/test_invocation_workspace_binding.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py` to remove the drained legacy execution adapter while retaining v3-only compile
- Modify: `packages/products/assurance-product/assurance_product/status.py` to remove live legacy Runtime projection
- Modify: `packages/products/assurance-product/assurance_product/application.py` to remove the drained legacy driver branch while retaining read-only historical evidence access
- Modify: `tests/architecture/test_legacy_workflow_deleted.py`
- Modify: `tests/architecture/legacy_import_inventory.py`
- Modify: `tests/architecture/test_legacy_import_inventory.py`
- Modify: `benchmark/agent-runtime-phase3/run_item.py`
- Modify: `scripts/graph_engine_smoke_test.sh`
- Modify: `.github/workflows/ci.yml`

**Task 7 retained-consumer migration set; Task 9 precondition:**

- Framework tests: `packages/framework/graph-engine/tests/runtime/test_activity_cancellation.py`, `test_activity_fold.py`, `test_activity_models.py`, `test_activity_port.py`, `test_activity_recovery.py`, `test_host_protocol.py`, `test_host_receipts.py`, `test_production_host.py`, `test_production_host_faults.py`, `test_production_host_security.py`, `test_secret_authorization.py`, `test_staged_promotion_recovery.py`, `test_task_workspace.py`, and `test_task_workspace_faults.py`. Move imports to `graph_engine.attempts.*`. Baseline `test_json_schema.py` already imports retained `graph_engine.json_schema`; Task 7 verifies it remains clean but does not restage it.
- Capability/adapter fixtures: `packages/capabilities/assurance-intake/tests/test_agent_skills.py`, `tests/agent_runtime/fakes.py`, `tests/agent_runtime/test_fixture_rebinding.py`, `tests/phase4/drift_helpers.py`, `tests/phase4/six_wheel_harness.py`, and `tests/phase6/test_phase2_invariant_reconciliation.py`.
- Product production/helper paths: `packages/products/assurance-product/assurance_product/runtime_ports.py`, `tests/product/cli_support.py`, `tests/product/composition_harness.py`, `tests/product/execution_loop.py`, `tests/product/product_runner.py`, `tests/product/public_closure.py`, and `tests/product/runtime_composition.py`.
- Product tests: `tests/product/test_product_runtime_ports.py`, `tests/product/test_agent_execution_contracts.py`, `test_change_runtime_layout.py`, `test_cli_fail_closed.py`, `test_cli_status_and_lock.py`, `test_generation_branches.py`, `test_no_whole_tree_residuals.py`, `test_replay_properties.py`, and `test_stop_and_interrupts.py`.
- Non-test consumers: `benchmark/agent-runtime-phase3/run_item.py` and `scripts/graph_engine_smoke_test.sh`. The benchmark must drive the new Attempt/Application harness or be explicitly retired from the checked benchmark manifest; because it is in the Pyright include set, leaving old imports is forbidden. The smoke script must construct and drive the migrated toy StateGraphs through Boot/Application and remain enabled in CI.

**Interfaces:** no custom planner, offered-token/activation readiness, scheduler wave, subgraph execution loop, legacy checkpoint projection authority, or engine-level `settle_next` loop.

- [ ] **Step 1: Extend deletion test to runtime authority.**

Assert production imports no legacy modules/symbols; no event model contains Workflow token offers/consumption/activation; no CLI imports `Engine`, old `Ledger.fold_events`, planner or scheduler. Attempt journal/effect state is retained but has no next-node API.

Before deleting the v2 runtime modules, extract the minimum read-only evidence surface to `graph_engine.evidence.legacy_v2`: byte-exact InvocationLock v2 authentication and legacy ledger/event integrity/fold needed to verify already completed/archived exports and `lock show`. It exposes no append, resume, next-node, planner, scheduler or effect-settlement operation. Golden, tamper and historical export tests prove the extracted reader accepts authentic v2 evidence and fails closed on corruption. Zero resumable legacy authorizes deletion of execution authority, not loss of historical verification.

- [ ] **Step 2: Verify every removed test already has replacement evidence.**

Task 7 must already have mapped every old suite to a passing replacement: route/join/fanout/interrupt → Feature/Product graph tests; activity/workspace/promotion/effects → Kernel tests; checkpoint/restart → anchored saver/Application tests; concurrency → lease/fence tests. Preserve security/fault tests that still exercise retained low-level code.

Before the atomic deletion starts, rerun Task 7's inventory. It must report zero imports of `graph_engine.runtime` and zero imports of the deleted `graph_engine.graph`/composition Workflow symbols across `packages`, `tests`, `examples`, `benchmark`, and Python embedded in `scripts`, excluding only the implementation modules, legacy characterization tests, and exact Product `application.py`/`cli.py`/`status.py` compatibility branches removed or rewritten by this Task. It must fail on read/parse errors rather than treating them as a clean scan. After those edits, rerun it with no allowlist and require an empty result.

- [ ] **Step 3: Remove settle loop only after six-effect Kernel tests pass.**

Legacy `runtime/effects.py` may be deleted/shrunk after no caller uses it and all six effect apply/reconcile/crash cases pass through the Kernel. There must not be two effect settlement authorities.

- [ ] **Step 4: Run framework/Product gates and commit.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence \
  packages/framework/graph-engine/tests/application \
  packages/framework/graph-engine/tests/evidence \
  packages/framework/graph-engine/tests/integration \
  packages/framework/graph-engine/tests/composition \
  packages/framework/graph-engine/tests/runtime \
  packages/capabilities/assurance-intake/tests/test_agent_skills.py \
  tests/agent_runtime \
  tests/phase4 \
  tests/phase6/test_phase2_invariant_reconciliation.py \
  tests/architecture/test_legacy_workflow_deleted.py \
  tests/architecture/test_legacy_import_inventory.py \
  tests/product/test_python_native_cutover.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_cli_status_and_lock.py \
  tests/product/test_composition_authority.py \
  tests/product/test_full_graph_audit.py \
  tests/product/test_no_whole_tree_residuals.py \
  tests/product/test_replay_properties.py \
  tests/product/test_stop_and_interrupts.py
uv run lint-imports
uv run pyright
bash scripts/graph_engine_smoke_test.sh
```

Stage every exact Task 9 `Files` path and every path in the retained-consumer migration set; inspect `git diff --cached --name-status`, then:

```bash
git commit -m "refactor: remove the custom Workflow Runtime"
```

### Task 10: Remove migration switches and update docs/packaging/smoke tests

**Files:**

- Delete: `packages/products/assurance-product/assurance_product/runtime_selection.py` after selection-record reading moves into the permanent Application identity component
- Modify: `packages/products/assurance-product/assurance_product/revision_registry.py` to retain only active graph-revision retention
- Modify: `packages/products/assurance-product/assurance_product/application.py` to remove the temporary runtime-selection/coexistence scaffolding while preserving Task 8's v3-only compile surface
- Modify: `.importlinter`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml` to retain all three smoke gates on the Python-native paths
- Modify: `scripts/graph_engine_smoke_test.sh`
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`
- Modify: `scripts/assurance_capability_wheel_smoke_test.sh`
- Modify: `tests/product/test_product_packaging.py`
- Modify: `tests/product/test_wheel_smoke_contract.py`
- Modify: `tests/product/test_project_configuration_security.py`
- Modify: `tests/product/test_cli_compile.py`
- Delete: `examples/minimal-product/aa_sample/_resources/schemas/workflow-schema.yaml`
- Delete: `examples/minimal-product/aa_sample/_resources/schemas/execution-contracts.yaml`
- Modify: `examples/minimal-product/pyproject.toml`
- Modify: `examples/minimal-product/aa_sample/product.py`
- Modify: `tests/phase6/test_final_wheel_metadata.py`

**Interfaces:** permanent Python-native rule; no runtime switch, YAML override promise or CLI graph authority; wheels contain factories and no topology YAML.

- [ ] **Step 1: Make migration controls fail the deletion gate.**

Add assertions that all new Invocations are LangGraph without a selection branch and `runtime_selection` is absent. Keep graph revision registry/retention because pinned resume is permanent, not migration-only. Assert that the ProductLock v3 plus `GraphBuildManifest` compile surface introduced in Task 8 is unchanged while the temporary coexistence selector/records are removed. Historical legacy evidence remains readable only through archive/evidence tooling retained by policy.

- [ ] **Step 2: Rewrite repository guidance.**

State: “Python wheels add capability and own `StateGraph` code; Product explicitly composes six Feature bundles; `.aa/` contains closed organization data only; changing nodes/edges requires code review, tests, wheel rebuild and authenticated deployment.” Remove promises for `.aa/workflow-schema.yaml`, `.aa/execution-contracts.yaml`, `workflow/module.yaml` and YAML graph replacement.

The non-loadable `examples/minimal-product` packaging fixture is not exempt from the final wheel-content rule. Delete both `_resources/schemas/workflow-schema.yaml` and `execution-contracts.yaml`, remove their package-data declaration/placeholder references, and update final wheel-metadata tests to prove no workspace wheel ships either obsolete orchestration contract.

Replace coexistence import-linter rules with the permanent boundary: cross-Feature `.graphs` imports remain forbidden, while obsolete `.workflow` suffix rules are removed because those packages no longer exist. Add a test that the config contains no legacy Workflow contract/module rule.

- [ ] **Step 3: Strengthen wheel smoke.**

Install built wheels in isolation; run `aa compile`; assert 14 roots/41 Attempt contracts/33 runtime bindings; inspect wheel members and reject `resources/workflow/module.yaml`, `resources/workflow/main.yaml`, `graph-inventory.yaml`; verify no SUT Python graph loads.

- [ ] **Step 4: Run the final negative inventory.**

```bash
test ! -e packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml
test ! -e packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml
test ! -e packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml
test ! -e packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml
test ! -e packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml
test ! -e packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml
test ! -e packages/products/assurance-product/assurance_product/resources/workflow/main.yaml
test ! -e packages/products/assurance-product/assurance_product/resources/graph-inventory.yaml
test ! -e tests/product/fixtures/workflow-module-ownership.yaml
test ! -e tests/fixtures/workflow-v2-minimal.yaml
uv run pytest -q tests/architecture/test_legacy_workflow_deleted.py tests/product/test_python_native_cutover.py
```

Expected: every command exits `0`. The architecture tests perform production-only import/symbol scans and report scanner I/O errors as failures; the shell gate does not use `! rg` or scan its own negative-test source.

- [ ] **Step 5: Run full repository release gate.**

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

Expected: all exit `0`, with no unexpected skip/xpass and no legacy wheel content.

- [ ] **Step 6: Commit permanent Python-native state.**

Stage only the migration-switch deletion, revision-registry/Application/import-linter edits, `AGENTS.md`, `README.md`, all three smoke scripts, the exact packaging/security/CLI tests, both minimal-product resource deletions plus its `pyproject.toml`/`product.py`, the Phase 6 wheel-metadata test, and the exact CI edit; inspect `git diff --cached --name-status`, then:

```bash
git commit -m "docs: make Python StateGraphs the sole workflow source"
```

## Product cutover completion gate

- [ ] Exactly 14 public roots compile from the authenticated Product/six Feature factory code and expose the exact public names.
- [ ] Product's four former `join:any` sites preserve current-trigger behavior; looped failure/coverage inboxes cover same-epoch late arrivals and replay; assessment exits prove outcome and predecessor mutual exclusion plus no late reactivation, or use the inbox fallback.
- [ ] All nine Feature/Product `join:any` rows are present and green with zero xfails or semantic waivers. Any failure blocks each not-yet-cut reachable root plus drain/deletion; it never switches an existing Invocation in place. The Product inbox fallback is an equivalent implementation, not an exception.
- [ ] The loop inventory still equals the exact seven `(graph_id, join:any node_id)` anchors derived from assembled `CompiledGraph.sccs`, with passing current-trigger evidence attached and full SCC membership available in failure diagnostics.
- [ ] Exactly one Generation `min_matches` site uses four-value `Send`; exactly two Intake sites are Feature-owned prepare/finalize two-consumer dataflow behind `ResolvedStructuredAgentExecutor`; no extra `Send`, generic fanout helper, compatibility executor, or phase shim exists.
- [ ] The authenticated test-only evidence Validator accepts/promotes and rejects/blocks promotion exactly once on both legacy and LangGraph paths, while shipped production contracts remain 25 registered / 0 bound.
- [ ] Standalone `improvement-evaluate` and the evaluate occurrence inside apply both emit `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`; committed/pending/publication-indeterminate recovery parity proves no early success or duplicate evaluator dispatch.
- [ ] The exact 50-site exclusive-route migration inventory is complete; every route's declared-fallback/multiple-match parameterization passes and no route uses first-match priority.
- [ ] All 13 human interrupts and system interrupt replay pass restart tests; multiple pending human interrupts require ID mapping.
- [ ] Checkpoints S0 and S are green. S cites the same exact S0 release identity or a newer identity that reran S0, and every `requires_structured_output=True` contract resolves a qualifying pinned `opencode_structured_output` adapter/server/provider/model/schema record before its entrypoint cutover; the non-promotable S0 report is not that record, and Kernel independently revalidates `info.structured`.
- [ ] Semantic shadow and crash matrix pass for all 14 entrypoints; no Invocation was dual-driven.
- [ ] Zero resumable legacy Invocations is authenticated before deletion, and every still-resumable pre-S LangGraph revision retains its original deployment artifact independently of the legacy drain result.
- [ ] No Workflow YAML, phase alias, compiler/projection DSL, planner/token scheduler, custom Workflow checkpoint authority or engine settle loop remains.
- [ ] `aa compile/start/run/resume/status/export/archive/bindings build/lock show` and wheel smoke pass against the Python-native Application.
- [ ] Full repository gate passes and a final code review verifies spec acceptance criteria and deletion inventory.
