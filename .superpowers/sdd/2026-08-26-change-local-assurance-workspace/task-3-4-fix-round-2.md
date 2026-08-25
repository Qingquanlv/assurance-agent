# Tasks 3 + 4 Fix Round 2

## Scope

This round fixes the remaining promotion-publication correctness bug and
records the precise Task 4 trust boundary. It changes only:

- `runtime/task_workspace.py`, `runtime/engine.py`, and `runtime/__init__.py`;
- their direct task-workspace, scheduler-fault, and engine tests;
- the approved design spec and implementation plan; and
- the Task 3/4/Fix Round reports.

It does not modify adapters, products, `graph_engine/__main__.py`,
`runtime/workspace.py`, or `runtime/tree_io.py`. Pre-existing Phase 4/5 dirty
work, including the `production_host.py`/production-host-fault lsof retry and
working-tree host golden, remains unstaged and unchanged by this round.

## TDD evidence

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_task_workspace_faults.py packages/graph-engine/tests/runtime/test_scheduler_faults.py packages/graph-engine/tests/runtime/test_engine.py -q --tb=short -k 'receipt_publication_fault or receipt_rename_reuses or durable_receipt_makes or rollback_failure_is_indeterminate or promotion_publication_indeterminate or engine_managed_successor'
```

Observed outcome: `11 failed, 121 deselected` in 1.63s.

The failures were specific:

- receipt temp write and file fsync, receipt rename, and receipt-directory fsync
  returned ordinary `OSError` after the canonical target contained staged
  bytes;
- successful canonical replacement deleted transaction files before publishing
  the completed receipt;
- pending unlink and receipts-directory fsync escaped even though the completed
  receipt already existed;
- rollback failure raised generic `TaskWorkspaceViolation` and deleted the
  backup needed for replay;
- scheduler had no dedicated publication-indeterminate type; and
- engine exposed the raw receipt exception and therefore had no explicit
  successor-gating contract at its API boundary.

### GREEN

Focused results:

- original RED selection: `11 passed, 121 deselected`;
- deterministic receipt-temp tamper regression: passed and rejected changed
  bytes without installing a completed receipt;
- `test_task_workspace_faults.py`: `19 passed`;
- task workspace + fault + scheduler fault + staged recovery + engine matrix:
  `150 passed` in 15.99s.

Full graph-engine command:

```text
uv run pytest packages/graph-engine/tests -q --tb=short
```

Authoritative outcome: `1295 passed, 1 skipped, 1 failed` in 82.07s.

The sole allowed failure is
`test_run_executes_only_the_explicit_product_plugin_bundle`; its traceback ends
at `graph_engine/__main__.py:288`, where Task 16 must supply
`InvocationWorkspaceBinding` to `Engine.start()`. The one skip is the existing
filesystem-dependent non-UTF-8 legacy workspace case.

A diagnostic direct `.venv/bin/pytest` run inside the restricted tool sandbox
could not enumerate production-host process descendants and consequently
reported ten host failures in addition to the CLI failure. The repository
command above was rerun with normal process-enumeration authority: its focused
production-host subset passed `22 passed`, and the authoritative full run has
only the Task 16 failure recorded above. No host code or pre-existing lsof retry
hunk was changed to accommodate the sandbox artifact.

Static checks:

- six changed Python files: `ruff check` → `All checks passed!`;
- the same files: `ruff format --check` → all six formatted;
- exact Task 3/4 production `pyright`, explicitly including
  `runtime/effects.py` → `0 errors, 0 warnings, 0 informations`.

## Finding A: receipt-first, fail-closed promotion publication

### Code mapping

- `PromotionPublicationIndeterminate` is a dedicated, exported prepared-state
  error. Scheduler does not catch or convert it to an ordinary failed attempt.
  Engine run, resume, and activity recovery translate it to
  `EnginePublicationIndeterminate`.
- Pending intent is durable before canonical mutation. Adjacent target temps and
  rollback backups remain deterministic, digest/mode authenticated, and fsynced
  before the first replacement.
- Rollback now proves every target equals its exact digest/mode baseline before
  permitting an ordinary error. Any rollback failure or unprovable target state
  raises `PromotionPublicationIndeterminate` and retains backups/temps/pending
  evidence for replay.
- Once all targets equal their staged digest/mode, the completed receipt is
  written to a deterministic name bound to identity and receipt digest. Existing
  temp content and mode are authenticated before reuse. The temp is fsynced,
  atomically renamed, the receipts directory is fsynced, and the installed
  receipt is parsed and compared.
- Only after durable completed receipt publication does best-effort cleanup
  remove transaction temps/backups, receipt temp residue, and pending intent.
  Cleanup failure cannot turn a committed promotion into an ordinary failure;
  completed-receipt replay retries every cleanup step.

### Test mapping

`test_task_workspace_faults.py` now covers:

- receipt write, receipt-file fsync, receipt rename, and receipt-directory fsync
  cuts after canonical replacement;
- deterministic receipt-temp reuse and tamper rejection;
- transaction cleanup unlink, pending unlink, and pending-directory fsync after
  completed receipt durability;
- rollback failure with a retained authenticated backup and successful replay;
- existing file-mode digest, apply, verify, and failure behavior.

`test_scheduler_faults.py` asserts the ledger remains at
`task_commit_prepared` with no `task_attempt_failed` or
`task_promotion_completed`. `test_engine.py` proves a canonically replaced file
cannot be consumed by an engine-managed successor before receipt/terminal
publication; resume completes the first promotion without re-executing its
handler and only then runs the successor.

### Atomicity wording

The approved direct `qa/changes/<id>` layout permits outputs in multiple
directories. There is no single OS primitive that atomically exposes that set.
The plan/spec now use these exact guarantees:

- each file uses adjacent-temp plus `os.replace` atomic publication;
- the batch uses durable intent/receipt, authenticated rollback/complete replay,
  resource locking, and fail-closed indeterminate state for terminal-failure
  atomicity;
- engine-managed readers consume only after receipt and terminal event; and
- a raw directory observer that ignores receipts can see a short intermediate
  set during successful replaces.

Generation pointers, SnapshotStore, candidate trees, and HEAD compatibility are
not reintroduced.

## Finding B: trusted handler versus untrusted provider

No fake TOCTOU defense was added.

- Installed `TaskHandler` and product Python is trusted runtime code; the SUT
  cannot register it.
- Task 4 host-v2 identities and pinned descriptors authenticate protocol inputs,
  reject persistent directory replacement, and reject worker path substitution.
- Pre/post pathname checks cannot sandbox a same-permission malicious installed
  handler that swaps, uses, and restores a path during its own call. Polling or
  more pre/post checks would not change that fact.
- Providers/models remain untrusted. Tasks 5 and 6 own the adapter and mechanical
  boundary: the real project path is readable, only authenticated `write_root`
  is writable, and OpenCode/OS-sandbox acceptance must reject provider
  shell/tool rename, replacement, and swap-use-restore attempts against the
  project root.

Existing persistent replacement and pinned-inode tests remain the Task 4
characterization. The plan's Task 4, Task 5, and Task 6 execution notes and the
design spec now state the boundary and Task 6 acceptance explicitly.

## Remaining risks and consciously deferred work

### Fix Round 3 correction

Fix Round 2 correctly moved the completed receipt ahead of cleanup, but its
single deterministic receipt temp conflated two states: a fully written,
authenticated prepared receipt and an incomplete construction interrupted by
real process exit. Therefore the earlier statements that changed temporary
bytes are always a conflict and that the deterministic temporary can always be
reused were too broad. A partial construction is not authenticated evidence of
a competing intent and must not make recovery permanently indeterminate.

Commits `7b844f459c85c8f338be1f28dffda23e41062126` and
`d6d5ff2cab33c8f263dab98531574208d751aefd` replace that protocol with a unique
construction file followed by a deterministic prepared file. Replay removes
and rebuilds partial/unparseable artifacts in the exact expected
identity/receipt-digest namespace, while preserving and rejecting a canonical
`PromotionReceipt` for a different intent. It also safely recognizes the exact
double-dot, identity-bound pending construction names left by Fix Round 2 so
repeated crashes do not accumulate orphans. The new process-crash and
public-recovery evidence is recorded in `task-3-4-fix-round-3.md`; this
correction supersedes Fix Round 2's deterministic-temp tamper wording, not its
receipt-first terminal semantics.

- The raw-filesystem multi-file visibility window is an accepted consequence of
  the direct layout. Engine-managed consumption is receipt/resource-lock gated.
- A persistently broken filesystem can leave authenticated cleanup residue after
  a durable receipt. Replay remains idempotent and continues cleanup; the
  terminal result does not regress.
- Malicious installed Python is outside the supported boundary. Untrusted
  provider confinement is deliberately deferred to planned Tasks 5 and 6, with
  explicit acceptance rather than a Task 4 path-check claim.
- Task 16 owns the single CLI binding failure and deletion of legacy
  Snapshot/tree/HEAD residual APIs. This round did not touch those files.
- No adapter/product migration or Fix Round Minor is hidden in this commit.

## Commits

- `4b6944c95a16d7db26a3b20add614e1e14b9f289` — atomic Tasks 3 + 4 cutover.
- `fe7502f55e389bc00374bd90c88f66e4b8c6aefd` — Fix Round 1 hardening.
- `cb21e3a027452943122292f4bb53697d13570474` — staged replacement for obsolete
  Snapshot crash skips.
- `5546e25c7b584463ed15fe996a33cf8481435b53` — Fix Round 2 receipt-first
  publication, explicit indeterminate recovery, reader gating, and plan/spec
  boundary clarification.
- `7b844f459c85c8f338be1f28dffda23e41062126` — Fix Round 3 interrupted
  construction recovery and public recovery translation.
- `d6d5ff2cab33c8f263dab98531574208d751aefd` — exact legacy pending-orphan
  recognition and cleanup.
