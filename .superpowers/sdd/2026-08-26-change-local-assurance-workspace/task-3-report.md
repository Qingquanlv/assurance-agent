# Task 3 Report — Staged Task Promotion

## Scope

Tasks 3 and 4 were implemented as one atomic cutover in commit
`4b6944c95a16d7db26a3b20add614e1e14b9f289`. Task 3 replaces scheduler
`SnapshotStore`/candidate-tree/HEAD commit semantics with `TaskWorkspaceStore`,
sealed `StagedWriteSet` validation, durable promotion intent/receipt recovery,
and digest-bearing terminal events. It also replaces assurance-shaped resource
parameters with closed, business-neutral `ResourceClaimTemplate` input
projections that resolve to concrete `ResourceClaims` before wave selection.

No adapter, product, `runtime/workspace.py`, or `runtime/tree_io.py` changes were
included. Existing Phase 4/5 dirty work was preserved.

## TDD evidence

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py -q
```

Outcome: `4 failed, 85 passed`. Failures showed the old scheduler accepting a
`SnapshotStore`, lacking `TaskWorkspaceStore.begin`, and publishing tree/HEAD
identity instead of staged-write/promotion identity.

### GREEN

Focused command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_scheduler_faults.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py -q --tb=short
```

Outcome: `195 passed, 6 skipped` in 20.14s. The six skips are Task 16
Snapshot/HEAD residual tests in `test_engine.py`; obsolete scheduler Snapshot
rollback/CAS skips were deleted rather than kept as a compatibility path.

Final graph-engine command:

```text
uv run pytest packages/graph-engine/tests -q --tb=short
```

Outcome: `1258 passed, 7 skipped, 1 failed` in 78.74s. The sole failure is the
explicitly deferred Task 16 CLI test
`test_run_executes_only_the_explicit_product_plugin_bundle`; its stack ends at
`graph_engine/__main__.py:288` where `_run_document` calls `Engine.start()`
without the now-required `workspace_binding`. Task 16 must construct and
re-supply `InvocationWorkspaceBinding(project_root, attempts_root,
receipts_root)` for CLI start/open/resume.

Static checks:

- Task-owned 41 changed Python files: `ruff check` passed; `ruff format --check`
  reported all 41 formatted.
- Task-owned graph-engine production files: `pyright` reported `0 errors, 0
  warnings`.
- Repository-wide `pyright` reported 151 later-task errors, concentrated in
  adapters/products and the explicitly deferred `__main__.py`, `effects.py`,
  and Task 16 `workspace.py` residuals. No Task 3/4 production file had an
  error.
- Graph-engine-wide ruff additionally reports two unchanged base-commit F401s
  in `graph/input_projection.py:209` and
  `tests/runtime/test_planner_input_projection.py:30`; neither file is in this
  task's write set.

## Changed files

Core Task 3 runtime/API files:

- `packages/graph-engine/graph_engine/plugin_api.py`
- `packages/graph-engine/graph_engine/graph/schema.py`
- `packages/graph-engine/graph_engine/graph/compiler.py`
- `packages/graph-engine/graph_engine/runtime/activity.py`
- `packages/graph-engine/graph_engine/runtime/engine.py`
- `packages/graph-engine/graph_engine/runtime/events.py`
- `packages/graph-engine/graph_engine/runtime/models.py`
- `packages/graph-engine/graph_engine/runtime/planner.py`
- `packages/graph-engine/graph_engine/runtime/scheduler.py`

Direct and graph-internal migration tests include the schema/compiler,
composition registry, toy integration, activity/reducer, effects, engine,
ledger/checkpoint, planner, scheduler, and secret-authorization suites, plus:

- `packages/graph-engine/tests/runtime/test_scheduler_faults.py` (new)
- `packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py` (new)

The complete authoritative file list is the 46-file stat of commit
`4b6944c95a16d7db26a3b20add614e1e14b9f289`.

## Semantic decisions

- Every attempt calls `TaskWorkspaceStore.begin`; tasks in one invocation share
  the authenticated project root while receiving distinct empty write roots.
- Scheduler order is begin, dispatch/reconcile, seal, validate the sealed staged
  set, durably publish `TaskCommitPrepared`, promote, publish
  `TaskPromotionCompleted`, then publish success (or settle durable effects).
- Promotion intent is durable before filesystem mutation. Recovery reads
  promotion receipts and finishes publication without rerunning the handler or
  repeating a completed promotion.
- Failed, stopped, indeterminate, rejected, expired, or undeclared-write
  attempts never promote. Validators receive `StagedWriteSet`, not a candidate
  tree.
- Terminal attempt/activity records use `staged_write_set_digest` and
  `promotion_receipt_digest`; failed/stopped attempts cannot carry a promotion
  receipt.
- Effect intents remain durable before success. After all receipts exist, the
  engine publishes success with the prepared staged and promotion digests.
- `ResourceClaimTemplate` accepts a closed parameter map of JSON-pointer-style
  input projections. Unknown parameters and slash, backslash, empty, or dot
  path components fail closed. Only resolved concrete `ResourceClaims` reach
  the scheduler.

## Remaining risks

- Task 16 still owns deletion of orphaned public Snapshot/tree classes and
  modules; they are not used by the new scheduler/engine execution path.
- Task 16 also owns CLI workspace binding, causing the one precisely attributed
  graph test failure above.
- Adapter/product consumers still compile against `workspace_root`, candidate
  write sets, or missing engine bindings; later planned tasks must migrate them.
- A pre-existing lsof retry hunk in `production_host.py` and its test were
  deliberately excluded from this commit. The staged execution-host golden was
  computed from the staged files, so the commit is self-consistent.

## Commit

- `4b6944c95a16d7db26a3b20add614e1e14b9f289` — atomic Task 3 + Task 4 runtime
  cutover.
