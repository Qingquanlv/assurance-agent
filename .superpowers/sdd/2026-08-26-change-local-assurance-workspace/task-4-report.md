# Task 4 Report — Authenticated Invocation Roots and Host Protocol v2

## Scope

Task 4 was implemented atomically with Task 3 in
`4b6944c95a16d7db26a3b20add614e1e14b9f289`. Invocation seed state is now
root-input-only; engine start/open require a process-local
`InvocationWorkspaceBinding`; invocation identity authenticates the project,
attempts, and receipts roots by digest; and host descriptor schema v2 binds the
project root, attempt write root, workspace identity, baseline, and staged write
set. Absolute paths remain process-local and are never serialized into ledger
events.

No adapter/product packages were modified. Existing Phase 4/5 dirty hunks were
preserved and excluded from the commit.

Fix Round 1 hardening is implemented by
`fe7502f55e389bc00374bd90c88f66e4b8c6aefd`, with obsolete Snapshot-era
blanket-skipped tests removed by
`cb21e3a027452943122292f4bb53697d13570474`. These results supersede the
original report wherever path-only identity or verification counts differ.

## TDD evidence

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py -q
```

Outcome: `7 failed, 23 passed`. Failures identified `WorkspaceSeed` and
`initial_tree_id` startup identity, optional engine binding, `TaskContext`'s old
single workspace root, and host `AttemptRootDescriptor(schema_version="1")`
that did not authenticate both roots.

### GREEN

The same focused suite with `--tb=short` completed with `30 passed` in 8.16s.

The final graph-engine suite completed with `1258 passed, 7 skipped, 1 failed`;
the only failure is the approved Task 16 CLI binding gap at
`graph_engine/__main__.py:288`, described in the Task 3 report.

Static verification:

- Task-owned 41 changed Python files passed ruff and format checks.
- Task-owned production files passed pyright with `0 errors, 0 warnings`.
- Repository-wide pyright reported 151 downstream/later-task migration errors;
  no Task 4 production file was among them.
- The staged execution-host implementation digest was independently recomputed
  from the four staged host files and exactly matched the staged invocation-lock
  golden: `42b3640c218147e17629b34e0120e87aef0bddda532a4b5b3bd15060366b9951`.

### Fix Round 1 RED/GREEN

The combined Fix Round RED command is recorded in the Task 3 report and in
`task-3-4-fix-round-1.md`; it produced `27 failed, 312 passed, 6 skipped`.
Task 4 failures specifically showed that canonical-path hashes accepted
same-path rename/recreate of the project, attempts, receipts, and attempt-leaf
directories, and that a worker could use a replaced project or write root after
a path-only check.

GREEN evidence:

- Same-path invocation-root, attempt-leaf, and worker substitution subset: `7
  passed`.
- Combined focused matrix: `359 passed, 6 skipped`.
- Engine/staged-promotion suites after deleting obsolete Snapshot-era blanket
  skips: `120 passed`, no skips.
- Final graph-engine suite: `1283 passed, 1 skipped, 1 failed` in 79.96s. The
  sole failure is the approved Task 16 CLI binding gap; the skip is the
  filesystem-dependent non-UTF-8 legacy workspace case.
- Exact task-owned ruff and format checks pass.
- Exact task-owned production pyright, including `runtime/effects.py`, reports
  `0 errors, 0 warnings, 0 informations`.

The staged execution-host digest for Fix Round commit
`fe7502f55e389bc00374bd90c88f66e4b8c6aefd` was recomputed from the Git index
and exactly matched the staged golden:
`bf35da190cefb514f591808607e2263b719b9d3ffa1726366aebc91ddf613f0e`.
The worktree-only lsof retry hunk remains unstaged and therefore has a separate
working-tree digest/golden without contaminating the commit.

## Changed files

Core Task 4 files:

- `packages/graph-engine/graph_engine/plugin_api.py`
- `packages/graph-engine/graph_engine/runtime/seed.py`
- `packages/graph-engine/graph_engine/runtime/engine.py`
- `packages/graph-engine/graph_engine/runtime/invocation_lock.py`
- `packages/graph-engine/graph_engine/runtime/host_protocol.py`
- `packages/graph-engine/graph_engine/runtime/production_host.py`
- `packages/graph-engine/graph_engine/runtime/production_worker.py`
- `packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py`
- `packages/graph-engine/tests/runtime/test_host_protocol.py`
- `packages/graph-engine/tests/runtime/test_production_host.py`
- `packages/graph-engine/tests/runtime/test_production_host_faults.py`

Graph-internal API migrations also updated the invocation lock golden and the
direct activity, receipt, seed, engine, security, and integration consumers.
The complete authoritative file list is commit
`4b6944c95a16d7db26a3b20add614e1e14b9f289`.

## Semantic decisions

- `InvocationSeed` schema v2 contains only `root_input` and its canonical
  digest. It has no project tree, initial tree, or path identity.
- `Engine.start()` and `Engine.open()` require
  `InvocationWorkspaceBinding(project_root, attempts_root, receipts_root)`.
  Each directory is resolved and authenticated locally; the start intent stores
  only canonical identity digests and authentication occurs before ledger read.
- Root identities bind canonical path digest plus no-follow directory
  `device`/`inode` evidence. Engine start/open and `TaskWorkspaceStore` compare
  fresh path evidence with pinned descriptors before reading or mutating
  authoritative state. Reopening a same-name attempt leaf authenticates the
  leaf identity and rejects replacement instead of returning the recorded
  identity.
- `TaskContext` exposes `project_root`, `write_root`, and
  `workspace_identity`. Host/worker code supplies exactly the roots authenticated
  by the descriptor.
- Host descriptor schema v2 includes `TaskWorkspaceIdentity`, stat-bound
  project/write-root evidence, root digests, baseline digest, and
  staged-write-set digest. The trusted host compares the descriptor with its
  pinned workspace binding. The worker opens and pins both descriptors,
  authenticates no-follow stat evidence before handler execution, and
  reauthenticates both entries before accepting the result.
- Terminal host receipts bind request, activity, both roots, baseline, staged
  write set, dispatch/reference identity, outcome, and quiescence proof.
- Replay and recovery consume durable terminal and promotion receipts without
  re-executing provider work. Activity reconciliation, cancellation, interrupts,
  STOP, tokens, and durable effects remain intact.
- The on-wire framing version remains the frozen host wire v1; the nested
  attempt-root descriptor is schema v2. The pinned host digest authenticates the
  implementation change.

## Remaining risks

- `TaskContext` intentionally remains path-based. The worker pins both root
  descriptors and checks entry identity before and after the handler, so a
  rename/recreate causes the call result to be rejected. The current handler
  API cannot force arbitrary provider path operations through `openat`; a
  malicious concurrent namespace writer can therefore create a transient path
  use window, though it cannot produce an authenticated accepted result. A
  future descriptor-native context would close that last architecture-level
  window.

- Task 16 must update `graph_engine/__main__.py` to create/re-supply the binding;
  until then the single CLI graph test fails exactly as recorded.
- Task 16 owns deletion of legacy `WorkspaceSeed`, `SnapshotStore`, tree I/O,
  orphaned HEAD event classes, and related exports. They remain isolated from
  the new engine/scheduler path because `workspace.py` and `tree_io.py` were
  explicitly outside this batch.
- Adapter and assurance-product packages still consume the previous context,
  seed, or host APIs and account for the downstream full-pyright failures.
- The worktree contains an unrelated pre-existing lsof timeout retry change in
  `production_host.py` and its fault test. Only Task 4 hunks were staged; the
  unrelated hunks remain in the worktree.

## Commit

- `4b6944c95a16d7db26a3b20add614e1e14b9f289` — atomic Task 3 + Task 4 runtime
  cutover.
- `fe7502f55e389bc00374bd90c88f66e4b8c6aefd` — Fix Round 1 promotion, effects,
  stat-bound root, attempt-leaf, and host-worker authentication hardening.
- `cb21e3a027452943122292f4bb53697d13570474` — remove obsolete Snapshot-era
  blanket-skipped crash tests after staged equivalents passed.
