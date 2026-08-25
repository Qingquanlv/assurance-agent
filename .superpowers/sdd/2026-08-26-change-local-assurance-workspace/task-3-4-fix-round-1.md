# Tasks 3 + 4 Fix Round 1

## Scope

This round addresses all five Important findings against atomic Tasks 3 and 4.
It stays inside graph-engine runtime/API files, their directly corresponding
tests, and the task reports. It does not modify adapters, assurance products,
`graph_engine/__main__.py`, `runtime/workspace.py`, or `runtime/tree_io.py`.

The pre-existing Phase 4/5 worktree changes were retained. In particular, the
lsof timeout retry in `runtime/production_host.py`, its import/test in
`test_production_host_faults.py`, and their working-tree host golden remain
unstaged. The committed host golden was computed independently from the Git
index.

## TDD evidence

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_effects.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/graph/test_schema_and_compiler.py packages/graph-engine/tests/runtime/test_planner.py -q --tb=short
```

Outcome: `27 failed, 312 passed, 6 skipped` in 37.98s.

The failures were retained before implementation and covered every Important
finding: partial promotion after the second replace, adjacent temp leakage on
write/fsync/mode errors, stale pending intent, mode omission, ordinary failure
after promotion, digest-less public effect success, same-path root and leaf
replacement, worker root replacement, mutable resource parameters, and unsafe
path substitutions.

### GREEN

- `uv run pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py -q`
  → `9 passed`.
- `uv run pytest packages/graph-engine/tests/runtime/test_effects.py -q`
  → `16 passed`.
- Engine effect subset → `5 passed, 112 deselected`.
- Same-path invocation-root/attempt-leaf/worker subset → `7 passed`.
- Resource immutability/unsafe substitution subset → `14 passed`.
- Combined focused matrix → `359 passed, 6 skipped`.
- `uv run pytest packages/graph-engine/tests/runtime/test_engine.py
  packages/graph-engine/tests/runtime/test_task_workspace_faults.py -q -rs`
  after deleting obsolete Snapshot-era blanket skips → `120 passed`.
- `uv run pytest packages/graph-engine/tests -q --tb=short` →
  `1283 passed, 1 skipped, 1 failed` in 79.96s. The one failure is the approved
  Task 16 CLI binding gap; the skip is the filesystem-dependent non-UTF-8
  legacy workspace case.
- Exact task-owned `ruff check` → `All checks passed!`.
- Exact task-owned `ruff format --check` → all files formatted.
- Exact task-owned production `pyright`, explicitly including
  `runtime/effects.py` → `0 errors, 0 warnings, 0 informations`.

Graph-engine-wide ruff still reports two unchanged F401s in
`graph/input_projection.py` and `test_planner_input_projection.py`. A broad
path-based pyright run reports existing Task 16, legacy workspace, downstream,
and test-fixture typing debt; it is not the configured task-owned gate. The
Fix Round production file set is clean.

## Findings mapped to code and tests

### 1. Promotion failure atomicity and mode

Code:

- `plugin_api.py`: `StagedFile` now binds before/after digest and POSIX mode;
  `TaskWorkspaceIdentity` and `StagedWriteSet` enforce complete digest/mode
  pairs.
- `runtime/task_workspace.py`: promotion persists the pending receipt before
  mutation; prepares/fsyncs deterministic adjacent replacement and rollback
  files for every target; applies mode; performs canonical replaces only after
  preparation; rolls changed files back in reverse order on any ordinary
  write/fsync/fchmod/replace failure; cleans temps/backups and newly created
  directories; and resumes authenticated pending transactions after a crash.
  A completed receipt removes a stale pending intent and transaction artifacts.

Tests in `test_task_workspace_faults.py` cover second-file replace rollback and
replay, write/fsync temp cleanup, unexpected third target state, completed
receipt pending cleanup, mode digest/application, fchmod failure, and
immediate-before-replace drift.

Mechanism: this is an authenticated durable rollback/complete transaction.
Declared outputs may span directories, so individual replaces have an
unavoidable short live visibility window. Fix Round 1 correctly prevented a
proven rollback from publishing an ordinary failed terminal outcome with a
partial canonical set, but its first implementation cleaned rollback evidence
before publishing the completed receipt and could surface an ordinary
filesystem exception after the canonical set was already changed. Fix Round 2
supersedes that publication order: the receipt becomes durable before cleanup,
and uncertainty stays prepared as `PromotionPublicationIndeterminate` until
authenticated replay.

### 2. Durable effects ordering and terminal state

Code:

- `runtime/events.py`, `runtime/models.py`, and `runtime/planner.py` add
  `TaskAttemptCommittedEffectFailed` / `committed_effect_failed`. It is
  non-retryable, carries staged and promotion digests, settles the remaining
  effect frontier, and can fail the node/graph without pretending promotion
  never happened.
- `runtime/effects.py` publishes that state for permanent apply/reconcile,
  invalid receipt, or exhausted policy. An ordinary `TaskAttemptFailed` is
  rejected after promotion.

Tests in `test_effects.py`, `test_engine.py`, and
`test_ledger_and_checkpoint.py` assert that promotion exists, ordinary failed
does not, both digests survive, remaining intents settle permanently, and no
retry is planned.

### 3. Public EffectExecutor publication

Code:

- `runtime/effects.py::EffectExecutor.settle_next` now obtains both success
  digests from `PreparedTaskCommit` and is the sole receipt-complete task
  publisher.
- The duplicate engine-private ready-success publisher was deleted from
  `runtime/engine.py`.

`test_public_executor_publishes_digest_bound_task_success_after_all_receipts`
directly exercises the exported executor and checks the event/projection. The
engine append-boundary test was updated to reflect the single public path.

### 4. Same-path root and attempt-leaf substitution

Code:

- `plugin_api.py` adds `DirectoryIdentity`, binding canonical path digest,
  no-follow `device`/`inode`, and a canonical identity digest.
- `InvocationWorkspaceBinding`, `TaskWorkspaceStore`, and `runtime/engine.py`
  capture/pin/authenticate project, attempts, receipts, and attempt-leaf
  descriptors. `TaskWorkspaceStore.begin` rejects a replaced same-name leaf.
- Host schema v2 carries trusted project/write stat evidence.
  `production_host.py` checks it against the store binding;
  `production_worker.py` pins both descriptors, authenticates before the
  handler, and reauthenticates before accepting the result.

Tests cover same-path rename/recreate of all three invocation roots, attempt
leaf replacement, and worker project/write-root replacement before handler
acceptance.

The handler-facing `TaskContext` remains path-based. Descriptor pinning plus
pre/post authentication rejects persistent substituted-root results. Installed
handlers are trusted runtime code, so these checks do not claim confinement
against a malicious same-permission handler that swaps and restores a path
during its own call. Provider/model code is untrusted; Tasks 5 and 6 must pass
the real project path through the authenticated adapter contract and enforce
read-only project access plus an OS/tool boundary whose only writable project
namespace is the authenticated attempt root.

### 5. ResourceClaimTemplate immutability and safe substitution

Code:

- `plugin_api.py` copies/sorts parameters into an immutable mapping and
  serializes it canonically.
- Planning rejects empty/dot/slash/backslash, control/DEL, Windows reserved
  devices, wildcard/stream characters, and trailing dot/space components
  before any attempt can be recorded.

Tests in `test_schema_and_compiler.py` prove external mutation cannot change
compiled planning semantics behind a stable digest. Parameterized planner
tests prove all unsafe substitutions fail before scheduler admission.

## Changed files

Fix implementation commit `fe7502f55e389bc00374bd90c88f66e4b8c6aefd`
changes 27 files:

- Runtime/API: `plugin_api.py`, `runtime/__init__.py`, `effects.py`, `engine.py`,
  `events.py`, `host_protocol.py`, `models.py`, `planner.py`,
  `production_host.py` (root-auth hunk only), `production_worker.py`,
  `scheduler.py`, and `task_workspace.py`.
- Tests/golden: invocation-lock golden; schema/compiler, toy integration,
  attempt workspace, effects, engine, host protocol, invocation lock/binding,
  ledger/checkpoint, planner, production-host fault/security, scheduler, and
  task-workspace fault tests.

Test cleanup commit `cb21e3a027452943122292f4bb53697d13570474`
removes six obsolete Snapshot/HEAD blanket-skipped cases from `test_engine.py`.
Their still-relevant durability, replay, rollback, root-authentication, and
concurrency invariants are exercised by the staged promotion and binding tests
listed above.

## Minor findings

- Completed-receipt replay now removes a stale pending journal and has a direct
  regression test.
- Obsolete blanket-skipped Snapshot crash tests were removed after their staged
  equivalents passed. No Fix Round Minor is consciously deferred.

## Remaining risks and out-of-scope work

- Task 16 must update `graph_engine/__main__.py` to construct/re-supply
  `InvocationWorkspaceBinding`. The sole allowed full-suite failure is
  `test_run_executes_only_the_explicit_product_plugin_bundle`, whose stack top
  is `graph_engine/__main__.py:288` calling `Engine.start()` without
  `workspace_binding`.
- Task 16 owns deletion of orphaned legacy Snapshot/tree APIs. This round did
  not touch `workspace.py`, `tree_io.py`, or the CLI.
- Adapter/product migrations remain later tasks.

## Commits

- `4b6944c95a16d7db26a3b20add614e1e14b9f289` — original atomic Tasks 3 + 4
  cutover.
- `fe7502f55e389bc00374bd90c88f66e4b8c6aefd` — Fix Round 1 implementation and
  regression tests.
- `cb21e3a027452943122292f4bb53697d13570474` — obsolete skipped-test cleanup.
- `5546e25c7b584463ed15fe996a33cf8481435b53` — Fix Round 2 correction to
  receipt publication order, explicit indeterminate recovery, and the trusted
  handler/provider boundary described above.
