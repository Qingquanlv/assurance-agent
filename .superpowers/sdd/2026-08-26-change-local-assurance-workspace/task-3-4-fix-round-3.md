# Tasks 3 + 4 Fix Round 3

## Scope

This round closes interrupted receipt-construction recovery and the public
`InvocationHandle.recover()` error boundary. It changes only:

- `packages/graph-engine/graph_engine/runtime/task_workspace.py`;
- `packages/graph-engine/graph_engine/runtime/engine.py`;
- their direct task-workspace fault and engine tests; and
- this report, the Task 3 report, and the Fix Round 2 correction.

It does not modify adapters, products, `graph_engine/__main__.py`,
`runtime/workspace.py`, `runtime/tree_io.py`, the existing production-host lsof
retry hunks, or any Phase 4/5 dirty work.

## TDD evidence

### RED

The regressions were written before the production change. Command:

```text
.venv/bin/pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_engine.py -q --tb=short -k 'partial_completed_receipt_construction or pending_intent_construction_crashes or public_recover_translates or replay_replaces_unparseable or pending_replay_preserves_authenticated_legacy'
```

Observed result: `5 failed, 131 deselected` in 0.78s.

The failures established that:

- a real `fork`/`os._exit` during completed-receipt construction left the
  canonical target fully promoted and the pending intent durable, but every
  fresh store treated the partial deterministic temp as a permanent conflict;
- two real process exits during pending-intent construction accumulated two
  random orphan temps;
- replay rejected an unparseable same-intent temp rather than rebuilding it;
- replay ignored an authenticated different-intent legacy pending construction
  instead of failing closed and preserving it; and
- direct `await handle.recover()` exposed
  `PromotionPublicationIndeterminate` rather than the engine-level error.

During the report audit, the legacy fixture was corrected to the exact
double-dot construction name produced by Fix Round 2's `_atomic_write_at`.
Before the legacy-prefix production correction, that direct regression was RED
with `1 failed, 22 deselected`: replay ignored the real historical orphan.

### GREEN

- Exact RED selection: `5 passed, 131 deselected` in 0.77s.
- `test_task_workspace.py`, `test_task_workspace_faults.py`, and
  `test_engine.py`: `153 passed` in 15.47s.
- Real process-crash selection: `2 passed, 22 deselected` in 0.34s.
- Direct public-recovery selection: `1 passed, 112 deselected` in 0.66s.
- Legacy cleanup/preservation plus real-crash selection:
  `4 passed, 20 deselected` in 0.48s.
- Full graph-engine suite:
  `1301 passed, 1 skipped, 1 failed` in 74.14s.

The sole failure is the approved Task 16 case
`test_run_executes_only_the_explicit_product_plugin_bundle`; its traceback ends
at `graph_engine/__main__.py:288`, where `_run_document` still calls
`Engine.start()` without `workspace_binding`. The skip is the existing
filesystem-dependent non-UTF-8 legacy-workspace case.

Static checks:

- the four changed Python files: `ruff check` → `All checks passed!`;
- the same files: `ruff format --check` → all four formatted; and
- exact Task 3/4 production `pyright`, explicitly including
  `runtime/effects.py` → `0 errors, 0 warnings, 0 informations`.

## Finding-to-code and test mapping

### Interrupted completed-receipt construction

Publication now has two distinct layers:

1. A unique construction name includes the workspace identity digest, canonical
   receipt digest, publication purpose, and a UUID token. The file is written
   completely with mode `0600`, file-fsynced, and its directory is fsynced.
2. Construction is atomically replaced into a deterministic prepared name with
   the same identity/receipt-digest/purpose binding, followed by a directory
   fsync.
3. Prepared is atomically replaced into the final pending or completed receipt,
   followed by a directory fsync and parsed receipt authentication.

Fresh replay scans only exact construction names in the expected namespace.
It safely removes incomplete or same-intent malformed artifacts and rebuilds
them. A complete expected prepared file is reused. A canonical receipt for a
different promotion intent is preserved and raises a workspace violation;
symlinks, hardlinks, and non-regular files fail no-follow/single-link checks and
are not unlinked.

`test_fresh_replays_replace_partial_completed_receipt_construction_and_finish`
uses a real child process to exit after the first partial receipt write. It
proves the target is fully after, pending exists, two consecutive fresh-store
replays return the same receipt, and all construction/prepared/pending residue
is gone.

### Pending-intent construction orphans

New pending-intent construction uses the same digest-bound two-layer protocol.
Replay also recognizes the old exact identity-plus-UUID pending construction
namespace. An unparseable partial legacy construction can be removed because
the name is attempt-identity scoped and regular-file authenticated; a valid
different-intent receipt is preserved and rejected.

`test_repeated_pending_intent_construction_crashes_do_not_accumulate_orphans`
uses two separate child exits and proves that replay removes the prior partial
before beginning the next construction, leaving at most one crash artifact and
none after successful replay.
`test_pending_replay_cleans_partial_legacy_construction` covers the exact
double-dot historical name, while the paired different-intent test proves that
valid competing evidence is preserved rather than cleaned.

### Public recovery error translation

`Engine._recover_invocation_claimed` now catches
`PromotionPublicationIndeterminate` from live-activity recovery and raises
`EnginePublicationIndeterminate`, matching run, resume, and activity-recovery
entry points. `test_public_recover_translates_promotion_publication_indeterminate`
exercises direct `await handle.recover()` rather than an internal scheduler
helper.

## Changed files

- `packages/graph-engine/graph_engine/runtime/task_workspace.py`
- `packages/graph-engine/graph_engine/runtime/engine.py`
- `packages/graph-engine/tests/runtime/test_task_workspace_faults.py`
- `packages/graph-engine/tests/runtime/test_engine.py`
- `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-3-report.md`
- `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-3-4-fix-round-2.md`
- `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-3-4-fix-round-3.md`

## Semantic decisions

- Construction files are expendable only when their exact name is bound to the
  expected attempt/receipt namespace and their inode is a private regular file.
  Prepared files are reusable only when bytes, canonical digest, and mode match.
- Authenticated different-intent evidence is never deleted. Replay fails closed
  so an operator can inspect it instead of silently overwriting a competing
  publication.
- Completed receipt durability remains the terminal authority. Cleanup after
  that point is best effort and replayable; no cleanup exception demotes an
  already committed attempt.
- No SnapshotStore, candidate tree, HEAD, generation pointer, adapter, product,
  or Task 16 CLI compatibility path was added.

## Remaining risks

- `os.fork` crash tests are skipped on platforms without fork semantics. The
  production protocol itself is platform-neutral over the existing `os.open`,
  `fsync`, and `replace` requirements.
- An unrelated or different-digest construction file is deliberately not
  removed. This may require operator inspection, but prevents cleanup from
  becoming an unauthenticated deletion primitive.
- The direct multi-directory layout retains the already documented short raw
  visibility window. Engine-managed readers remain receipt/terminal-event gated.
- Task 16 still owns the one CLI binding failure; adapters and products remain
  outside this round.

## Commits

- `4b6944c95a16d7db26a3b20add614e1e14b9f289` — atomic Tasks 3 + 4 cutover.
- `fe7502f55e389bc00374bd90c88f66e4b8c6aefd` — Fix Round 1 hardening.
- `5546e25c7b584463ed15fe996a33cf8481435b53` — Fix Round 2 receipt-first
  publication and explicit indeterminate recovery.
- `7b844f459c85c8f338be1f28dffda23e41062126` — Fix Round 3 two-layer
  construction recovery and public recovery translation.
- `d6d5ff2cab33c8f263dab98531574208d751aefd` — historically exact legacy
  pending-orphan recognition, cleanup, and fail-closed authentication.
