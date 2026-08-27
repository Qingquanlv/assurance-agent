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

Fix Round 2 is implemented by
`5546e25c7b584463ed15fe996a33cf8481435b53`. It leaves the persistent
replacement and pinned-inode defenses intact while correcting the threat claim:
installed handlers are trusted runtime code, and Task 4 does not claim to
sandbox a malicious same-permission handler that swaps and restores a path
during its own call.

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

### Fix Round 2 evidence

The combined RED/GREEN commands and counts are recorded in the Task 3 report
and `task-3-4-fix-round-2.md`. Task 4-specific assertions prove that promotion
publication uncertainty is translated by engine run/resume/activity-recovery
paths and that a successor cannot consume a canonically replaced file before
the completed receipt and terminal event.

No polling, repeated pre/post check, descriptor-only fake path API, adapter
change, or malicious-installed-handler test was added. Existing persistent
same-path replacement and pinned inode tests remain green in the full
graph-engine suite. The plan/spec now make Tasks 5 and 6 responsible for the
untrusted provider boundary, including shell/tool swap-use-restore acceptance.

Final Fix Round 2 verification is `1295 passed, 1 skipped, 1 failed` in 82.07s;
the sole failure remains the approved Task 16 CLI binding gap. Task-owned ruff,
format, and production pyright (including `runtime/effects.py`) all pass.

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
- Installed `TaskHandler` and product Python is trusted. Host-v2 root
  authentication guarantees protocol binding, persistent namespace replacement
  detection, and worker path-substitution rejection; it is not an OS
  confinement boundary against malicious trusted code that actively restores
  the authenticated pathname before returning.
- Provider/model code is outside that trusted boundary. Tasks 5 and 6 must pass
  the real project path as read context while enforcing a read-only project and
  making only the authenticated `write_root` writable. Task 6 acceptance now
  explicitly covers provider shell/tool rename, replacement, and
  swap-use-restore attempts.
- Terminal host receipts bind request, activity, both roots, baseline, staged
  write set, dispatch/reference identity, outcome, and quiescence proof.
- Replay and recovery consume durable terminal and promotion receipts without
  re-executing provider work. Activity reconciliation, cancellation, interrupts,
  STOP, tokens, and durable effects remain intact.
- The on-wire framing version remains the frozen host wire v1; the nested
  attempt-root descriptor is schema v2. The pinned host digest authenticates the
  implementation change.

## Remaining risks

- `TaskContext` intentionally remains path-based because external providers
  need a real project path. Persistent external replacement is rejected by
  pinned descriptors and current-entry authentication. Swap-use-restore by a
  malicious installed handler is outside Task 4's supported threat model;
  describing pre/post checks as preventing it would be false assurance.
- Provider confinement is incomplete until Tasks 5 and 6 land their adapter
  request and OpenCode/OS sandbox boundaries. Their acceptance must prove
  provider shell/tool attempts cannot mutate or swap the project root and can
  write only the authenticated attempt root.

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
- `5546e25c7b584463ed15fe996a33cf8481435b53` — Fix Round 2 indeterminate
  promotion translation and explicit trusted-handler/provider threat boundary.
