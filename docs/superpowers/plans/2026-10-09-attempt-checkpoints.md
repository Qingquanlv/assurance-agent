# Attempt Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace the Attempt event journal and action selector with complete persisted checkpoints and direct phase handlers, including every existing reader.

**Architecture:** Checkpoint storage atomically replaces a typed record using a revision and live fencing token. Runtime dispatches its persisted phase; handlers own transaction boundaries. Product projections consume retained checkpoints rather than events.

**Tech Stack:** Python 3.11, Pydantic, SQLite/aiosqlite, LangGraph, uv, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-attempt-checkpoints-design.md`

## Global Constraints

- Python 3.11 and the existing uv workspace. No new dependency.
- Python wheels own graph topology, handlers, contracts and policy. Do not load implementations from the SUT.
- Flow topology, technical Attempt identity, retry budgets, business loop budgets and public resolution meanings remain unchanged.
- Keep the existing single-worker admission, live authority checks, workspace validation and commit/release proofs.
- LangGraph checkpoints, checkpoint anchors/outbox and resource authorization records are separate mechanisms; this change does not delete them.
- The checkpoint is the only Attempt execution state. No dual writes to an event stream, compatibility journal facade, or new generic effect layer.
- Keep every Attempt's final checkpoint. A later successful Attempt must not overwrite an earlier failed Attempt.
- User approved the design and immediate implementation. Do not stop for another design or execution-method confirmation.

## Review Focus

- A crash after dispatch binding must observe/cancel the same external call, not launch another one.
- A crash after file promotion must complete that Attempt without treating the write as an uncommitted failure.
- Stale revision or fencing token must fail without partially changing checkpoint or authorization state.
- A pending/unknown handler must yield; same-phase durable progress must still be accepted.
- Successful retry must not erase failed Attempt evidence, and old journal-format runs must fail clearly without being deleted.

## Task 1: Typed checkpoint and storage adapters

**Files:**
- Create `packages/framework/graph-engine/graph_engine/attempts/checkpoint.py`.
- Create `packages/framework/graph-engine/graph_engine/persistence/attempt_checkpoint.py`.
- Create `packages/products/assurance-product/assurance_product/sqlite_attempt_checkpoint.py`.
- Modify `packages/products/assurance-product/assurance_product/sqlite_checkpointer.py` to support checkpoint rows alongside the old table until Task 2 switches callers.
- Create focused memory/SQLite tests in the respective persistence and product test directories.

**Interfaces:**
The checkpoint module exports `AttemptCheckpoint`, `AttemptPhase`, `AttemptResult`, `ActiveSystemInterrupt`. `AttemptResult` carries the old terminal-result data without being an event. Preserve complete identity, activity, workspace proof, terminal and interrupt information represented by the existing snapshot; phase is explicit, never derived from facts.

The persistence module exports `AttemptCheckpointStore`, `MemoryAttemptCheckpointStore`, `AttemptCheckpointIntegrityError` and canonical encode/decode helpers. The product adapter exports `SqliteAttemptCheckpointStore`. The store offers:

```python
async def load(self, attempt_key: AttemptKey) -> AttemptCheckpoint | None: ...
async def commit(self, checkpoint: AttemptCheckpoint, *, expected_revision: int, fencing_token: int) -> AttemptCheckpoint: ...
async def read_checkpoints(self) -> tuple[AttemptCheckpoint, ...]: ...
async def ensure_durable(self, attempt_key: AttemptKey) -> None: ...
```

An absent record has revision zero; each successful commit returns revision `expected_revision + 1`. Retain the exact current `latest_generation` and `register_generation` call signatures on both adapters. The store validates complete records, immutable identity, monotonic lifecycle facts, CAS and live fencing. Snapshot replacement cannot erase a terminal outcome, promotion proof, release proof or acknowledged interruption. No event-list interface or `append` adapter is allowed.

Memory storage keeps a no-argument constructor, enforces stored-fence monotonicity and accepts an optional live-fence assertion callback. SQLite always checks its backend runner lease. Runtime/resource-arbiter live checks remain in both execution paths; generic boot need not invent a runner lease before an invocation exists.

- [x] Read existing snapshots, terminal payloads, persistence CAS/fence checks, generation allocation and their tests.
- [x] Add tests that commit a checkpoint, reconstruct a new adapter, load the same phase/data and retain another Attempt's failed result; stale revision/token and malformed phase/data must fail without overwriting valid bytes.
- [x] Run those tests and record the expected missing-feature failure.
- [x] Implement model and memory storage with canonical defensive copying. Use an explicit durable phase enum with authorization, execute, reconcile, commit, terminate, release and done stages; enum spelling can follow repository conventions but must be reported for Task 2.
- [x] Implement SQLite storage and schema, including generation preservation and transaction rollback. Keep existing callers working during this intermediate commit; do not run or write both storage models for a live Attempt.
- [x] Run new tests plus existing SQLite, generation and persistence regressions. Run Ruff and formatting for changed files.
- [x] Commit, self-review and report the concrete model/signatures, tests and commit range for independent review.

## Task 2: Switch the runtime and all readers, then delete the journal

**Files:**
- Migrate `graph_engine/attempts/{runtime,kernel,handlers,commit,activity,node_factory,checkpoint_bridge,context,__init__}.py` and `graph_engine/boot/{boot,generic}.py`.
- Migrate product `runtime_ports.py`, `retained_host.py`, `worker_cleanup.py`, `operator_views.py`, `status.py`, `application.py`, `retro_evidence.py`, `sqlite_checkpointer.py` and affected runtime bindings.
- Update `assurance_improvement/contracts/retro.py`, runtime snapshot/evidence readers and their fixtures for the versioned checkpoint evidence document.
- Migrate graph harnesses and all tests importing old Attempt events, snapshots or journal; delete the journal golden fixture only after replacing its behavioral coverage.
- Delete `graph_engine/attempts/events.py`, old derived `phase.py`, `graph_engine/persistence/attempt_journal.py` and product `sqlite_attempt_store.py` after all consumers move.
- Update README and current runtime architecture docs; preserve historical descriptions only when clearly historical.

**Interfaces:**
Consume Task 1's store and model. `AssuranceAttemptKernel` exposes/injects `checkpoints`, not `journal`. Runtime reads `checkpoint.phase`, invokes a phase handler and reloads after durable progress. Preserve public resolution and activity-port interfaces. Replace `ContinueAttempt(snapshot, ...)` with an explicit durable-progress result and keep a distinct return/yield result.

```python
checkpoint = await handlers.open_or_restore()
while True:
    result = await handlers.phases[checkpoint.phase](checkpoint)
    if isinstance(result, ReturnResolution):
        return result.resolution
    latest = await checkpoints.load(checkpoint.attempt_key)
    if latest is None or latest.revision <= checkpoint.revision:
        raise RuntimeError("Attempt handler made no durable progress")
    checkpoint = latest
```

The exact class locations can follow the existing cohesive handlers. This loop specifies ownership and progress behavior; it must not reintroduce an action selector or permit uncontrolled polling. Restore transient workspace and authorization prerequisites on each entry, including recovery straight into commit/release, without persisting handles or resetting the durable phase.

- [x] Add direct phase/restart, pending/no-progress and crash-boundary tests and observe the expected failures before switching the runtime.
- [x] Switch handlers to full checkpoint commits. Before invoking external business work, commit its recovery phase; after observed results, atomically store the next phase and data. Preserve promotion and release proof barriers.
- [x] Switch the activity port, generation allocation, system interruption bridge and boot/test harnesses. Preserve retry identity/budgets and current single-worker semantics.
- [x] Migrate retained-host dispatch/cancel ownership checks and legacy-format admission. Old nonempty journal-format databases remain intact and cannot silently start a new Attempt; documentation specifies a fresh isolated run.
- [x] Migrate status, session/output projections and post-run evidence. Retro tests include two failures followed by success, different input/node/invocation isolation, absence/incomplete evidence, safe fingerprints and immutable pre-Retro artifact selection.
- [x] Remove production event/journal/action implementations, imports and obsolete fixtures. No test-only journal compatibility layer in production. Keep crash/fence/retry tests as checkpoint-based behavioral tests.
- [x] Run focused tests across framework attempts/persistence/Flow and product store/Retro/status/stop. Record all unresolved failures; do not declare a partial migration complete.
- [x] Run all repository gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, `uv run pytest`, and the three smoke scripts. Use `uv run` and execute smoke scripts as documented in CI.
- [x] Commit coherent fixes, self-review, and report exact test outputs and changed interfaces. An independent task review and whole-branch review follow.

## Controller verification

- [x] Review each task's commit range against the spec and interfaces before starting its dependent task.
- [x] Independently verify final code uses persisted phase dispatch and contains no old Attempt journal execution path.
- [x] Verify the final repository gate covers the final tree; rerun only tests affected by subsequent fixes, and run the full gate if final changes invalidate the recorded run.
- [x] Present the worktree/branch, migration result, old-run limitation and actual checks. Do not create a PR unless requested.


## Completion record

Implemented from remote main `abd86c34a3dfbe4bc1d412724d0b50c8ef636147` in the `codex/attempt-checkpoints` worktree. The final verified code is `7eaa08babd3309f71a5c8068240d541f34ba2136`; this completion record changes documentation only.

- Complete checkpoints are the sole Attempt state; persisted phases directly select handlers.
- Activity recovery, interruptions, retained cancellation, status, output associations and Retro V2 consume checkpoints. Old Attempt events, action selection and journal implementations are deleted.
- Independent storage and runtime reviews passed. Final review found and fixed historical-anchor delivery authority and ownerless history admission; scoped re-review approved both fixes.
- Full `uv run pytest -x -q --tb=short`: **4807 passed, 18 skipped in 616.86s** on the frozen final code.
- Generated declarations, Ruff lint/format, Pyright and all 32 import contracts passed. Pyright retains one existing `jsonschema` missing-source warning and no errors.
- Graph engine, capability wheel and product wheel smoke scripts all passed on the same final code.
- Nine added recovery/admission regressions passed; the associated 149-test integration set passed. Test-only SQLite restart fixture correction also passed its six-test suite.
- Branch and worktree remain available locally. No push, PR or merge was requested for this migration.

## Migration decisions and costs

1. Preserve old journal-format data but reject execution of a nonempty old-format database. Stop its run with the original version and start a fresh isolated run with rebuilt wheels. This removes the old replay runtime; the cost is no in-place resume of old-format runs.
2. Keep no-argument memory storage with optional live-fence injection for generic boot and test harnesses. Stored-fence monotonicity always applies, SQLite always checks its live lease, and runtime arbiter checks remain. The cost is that a bare memory store does not independently prove live ownership; those tests must inject the live-fence callback.
3. Preserve the product's existing `recover_outbox`/`recover_handshake` wiring and conservative Retro ordering for different Attempts sharing one terminal fence. They are unchanged behavior outside this migration. The cost is retaining the existing recovery-entry limitation and an unknown recovery classification when ordering cannot be proved. The confirmed historical-anchor `arecover` authority defect is fixed and covered with real SQLite restart tests.
