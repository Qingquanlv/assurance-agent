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

Fix Round 1 hardening is implemented by
`fe7502f55e389bc00374bd90c88f66e4b8c6aefd` with obsolete Snapshot-era skipped
tests removed by `cb21e3a027452943122292f4bb53697d13570474`. These commits supersede the
original report wherever promotion atomicity, effect-failure state, root
authentication, resource-template immutability, or verification counts differ.

Fix Round 2 publication ordering and recovery are implemented by
`5546e25c7b584463ed15fe996a33cf8481435b53`. It supersedes Fix Round 1's
over-broad claim that every post-replace filesystem exception had a proven
terminal outcome. Completed receipts now precede cleanup, and an uncertain
replacement, rollback, or receipt publication remains prepared as
`PromotionPublicationIndeterminate`.

Fix Round 3 interrupted-construction recovery is implemented by
`7b844f459c85c8f338be1f28dffda23e41062126`, with historically exact legacy
pending-orphan recognition in `d6d5ff2cab33c8f263dab98531574208d751aefd`.
These supersede Fix Round 2's single-file deterministic receipt-temporary
protocol, which could mistake a partial process-crash artifact for a conflicting
authenticated publication. Receipt and pending publication now use separate
construction and prepared layers, and direct public recovery translates
promotion uncertainty at the engine boundary.

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

### Fix Round 1 RED/GREEN

RED command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_effects.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/graph/test_schema_and_compiler.py packages/graph-engine/tests/runtime/test_planner.py -q --tb=short
```

Outcome: `27 failed, 312 passed, 6 skipped`. The failures demonstrated partial
multi-file visibility after an ordinary promotion error, leaked temporary
files, missing mode authentication, ordinary `TaskAttemptFailed` after a
durable promotion receipt, a digest-less public `EffectExecutor` success path,
same-path rename/recreate attacks, a replaced attempt leaf, mutable resource
parameters, and unsafe control/non-portable substitutions.

GREEN evidence:

- Promotion fault suite: `9 passed`.
- Public effect executor suite: `16 passed`; engine effect subset: `5 passed`.
- Root/attempt-leaf same-path suite: `7 passed`.
- Resource-template immutability/unsafe-component suite: `14 passed`.
- Combined focused matrix: `359 passed, 6 skipped`.
- Engine plus staged-promotion suite after removing obsolete Snapshot-era
  blanket skips: `120 passed` with no skips.
- Final graph-engine suite: `1283 passed, 1 skipped, 1 failed` in 79.96s. The
  sole failure is the approved Task 16 CLI binding gap; the skip is the
  filesystem-dependent non-UTF-8 legacy workspace case.
- Exact task-owned ruff and format checks pass; exact Task 3/4 production
  pyright, explicitly including `runtime/effects.py`, reports `0 errors, 0
  warnings, 0 informations`.

The broader `pyright packages/graph-engine/graph_engine
packages/graph-engine/tests` command is not a clean project gate in this
worktree: it reports existing later-task/test typing debt, including Task 16
`__main__.py`, legacy `workspace.py`, and many test-fixture protocol errors.
The exact task-owned production command above is the authoritative Fix Round
check.

### Fix Round 2 RED/GREEN

RED command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_scheduler_faults.py packages/graph-engine/tests/runtime/test_engine.py -q --tb=short -k 'receipt_publication_fault or receipt_rename_reuses or durable_receipt_makes or rollback_failure_is_indeterminate or promotion_publication_indeterminate or engine_managed_successor'
```

Outcome: `11 failed, 121 deselected` in 1.63s. Receipt write/fsync/rename and
receipt-directory fsync surfaced ordinary `OSError` after canonical mutation;
transaction cleanup ran before the completed receipt; pending cleanup failures
escaped after a durable receipt; rollback failure used a generic workspace
error and deleted recovery evidence; and engine did not translate promotion
publication uncertainty.

GREEN evidence:

- The RED selection passed all `11` cases; the deterministic receipt-temp
  tamper regression also passes and rejects changed bytes.
- Task-workspace fault suite: `19 passed`.
- Workspace/promotion/scheduler/engine focused matrix: `150 passed` in 15.99s.
- Final full graph-engine suite: `1295 passed, 1 skipped, 1 failed` in 82.07s.
- The only full-suite failure remains Task 16's CLI binding gap at
  `graph_engine/__main__.py:288`; the skip remains the filesystem-dependent
  non-UTF-8 legacy workspace case.
- Fix Round 2's six changed Python files pass `ruff check` and
  `ruff format --check`.
- Exact Task 3/4 production `pyright`, explicitly including
  `runtime/effects.py`, reports `0 errors, 0 warnings, 0 informations`.

### Fix Round 3 RED/GREEN

RED command, run after adding the process-crash and public-API regressions but
before changing production code:

```text
.venv/bin/pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_engine.py -q --tb=short -k 'partial_completed_receipt_construction or pending_intent_construction_crashes or public_recover_translates or replay_replaces_unparseable or pending_replay_preserves_authenticated_legacy'
```

Outcome: `5 failed, 131 deselected` in 0.78s. A partial deterministic completed
receipt temp permanently conflicted on every fresh replay; random pending-intent
construction temps accumulated across real `fork`/`os._exit` cuts; the old
implementation ignored an authenticated different-intent legacy construction;
and direct `await handle.recover()` leaked
`PromotionPublicationIndeterminate`.

The report audit then corrected the legacy regression fixture to the exact
double-dot name created when Fix Round 2 passed an already-dot-prefixed pending
name to `_atomic_write_at`. That historically exact test produced an additional
RED of `1 failed, 22 deselected`: the real legacy orphan was ignored.

GREEN evidence:

- the exact RED selection: `5 passed, 131 deselected`;
- task workspace, task-workspace faults, and engine: `153 passed` in 15.47s;
- the two real process-crash tests: `2 passed, 22 deselected`;
- direct public recovery translation: `1 passed, 112 deselected`;
- legacy and real-crash cleanup/preservation selection:
  `4 passed, 20 deselected`;
- final graph-engine suite: `1301 passed, 1 skipped, 1 failed` in 74.14s;
- the same sole Task 16 CLI failure at `graph_engine/__main__.py:288`, with the
  same filesystem-dependent non-UTF-8 legacy-workspace skip;
- the four changed Python files pass exact `ruff check` and
  `ruff format --check`; and
- exact Task 3/4 production `pyright`, explicitly including
  `runtime/effects.py`, reports `0 errors, 0 warnings, 0 informations`.

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
- `StagedFile`, staged digests, baseline identity, and promotion receipts bind
  both content digest and POSIX mode. Promotion applies and authenticates the
  sealed mode instead of forcing `0600`.
- A promotion first persists its pending intent, then prepares and fsyncs every
  adjacent replacement and rollback file before the first canonical replace.
  An ordinary write/fsync/replace/mode error is returned only after rollback
  proves every canonical target is at its baseline. If that proof fails,
  `PromotionPublicationIndeterminate` preserves the pending record and
  deterministic rollback evidence for replay.
- Once every target is proven staged, receipt publication writes a unique
  identity/receipt-digest/purpose-bound construction file, applies mode `0600`,
  fsyncs it, and atomically replaces it into a deterministic prepared name.
  After the receipts directory is durable, that prepared file is atomically
  replaced into the completed receipt and the directory is fsynced again. Only
  then does best-effort cleanup remove target temps/backups, publication
  artifacts, and the pending intent. Cleanup failure cannot demote the
  completed promotion; replay retries residue cleanup.
- Replay may remove only exact construction/prepared names in the authenticated
  promotion namespace. Partial or unparseable same-intent artifacts are rebuilt;
  a valid receipt for a different intent is preserved and rejected. Regular-file,
  single-link, no-follow checks prevent cleanup from following or deleting an
  attacker-selected filesystem object.
- Scheduler leaves an indeterminate publication at `TaskCommitPrepared` with
  no ordinary failure or promotion-completed event. Engine translates it to
  `EnginePublicationIndeterminate`; resume completes from authenticated
  pending/backup/receipt evidence without handler re-execution. Successors do
  not consume the canonical files until receipt and terminal event publication.
- Failed, stopped, indeterminate, rejected, expired, or undeclared-write
  attempts never promote. Validators receive `StagedWriteSet`, not a candidate
  tree.
- Terminal attempt/activity records use `staged_write_set_digest` and
  `promotion_receipt_digest`; failed/stopped attempts cannot carry a promotion
  receipt.
- Effect intents remain durable before success. The exported public
  `EffectExecutor` is the sole publisher after receipts exist and publishes
  success with the prepared staged and promotion digests. A permanent effect
  error after promotion publishes `TaskAttemptCommittedEffectFailed`, never an
  ordinary failed attempt; it retains both digests and is never retried.
- `ResourceClaimTemplate` accepts a deeply immutable closed parameter map of
  JSON-pointer-style input projections. Unknown parameters and empty, dot,
  slash, backslash, NUL/control, reserved-device, wildcard, stream, or trailing
  dot/space components fail during planning. Only resolved concrete
  `ResourceClaims` reach the scheduler, and external mutation cannot change
  compiled planning semantics behind a stable digest.

## Remaining risks

- Arbitrary declared files may span multiple directories in the required
  direct `qa/changes/<id>` layout. No single OS primitive can make those
  replaces simultaneously visible. Each file replace is atomic; the batch has
  terminal-failure atomicity through durable intent/receipt, resource locks,
  and fail-closed replay. Engine-managed readers wait for receipt and terminal
  event, but a raw filesystem observer that ignores receipt state can see a
  short intermediate set. Generation/pointer, SnapshotStore, tree, and HEAD
  indirection remain deliberately excluded.
- Persistently failing cleanup may leave authenticated temp/backup/pending
  residue after a completed receipt. This cannot change the terminal outcome;
  later replay continues cleanup.

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
- `fe7502f55e389bc00374bd90c88f66e4b8c6aefd` — Fix Round 1 promotion, effects,
  root-identity, and resource-template hardening.
- `cb21e3a027452943122292f4bb53697d13570474` — remove obsolete Snapshot-era
  blanket-skipped crash tests after staged equivalents passed.
- `5546e25c7b584463ed15fe996a33cf8481435b53` — Fix Round 2 receipt-first
  publication, explicit indeterminate recovery, successor gating tests, and
  direct-layout/threat-boundary clarification.
- `7b844f459c85c8f338be1f28dffda23e41062126` — Fix Round 3 two-layer
  publication construction, crash-orphan recovery, and public recovery error
  translation.
- `d6d5ff2cab33c8f263dab98531574208d751aefd` — recognize, clean, and
  fail-closed authenticate the exact double-dot pending-orphan namespace left
  by Fix Round 2.
