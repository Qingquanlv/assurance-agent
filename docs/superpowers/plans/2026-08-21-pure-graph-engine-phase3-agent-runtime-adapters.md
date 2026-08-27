# Pure Graph Engine Phase 3 Agent Runtime Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a business-neutral recoverable task-activity seam to Graph Engine and ship independent OpenCode and Cursor runtime-adapter wheels that execute the same frozen provider-neutral `AgentRunRequest` without moving Assurance behavior or cutting over `aa`.

**Architecture:** Graph Engine owns only generic activity events, immutable attempt identity, confined lifecycle calls, durable host terminal receipts, reconciliation, cancellation, and same-attempt adoption. `agent-runtime-contracts` owns frozen request/result values. OpenCode and Cursor live in independent plugin wheels and implement the same `RecoverableTaskHandler` contract while preserving their different recovery guarantees. A neutral fixture capability proves explicit rebinding and wheel isolation.

**Tech Stack:** Python 3.11, Pydantic v2, asyncio, descriptor-relative POSIX filesystem operations, `httpx` with SSE/polling fakes, subprocess `stream-json`, pytest, Hypothesis-style property matrices implemented with pytest parametrization, Ruff, Pyright, import-linter, uv/hatchling wheels.

## Global Constraints

- Work only in `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec` on `codex/pure-graph-engine-phase3-spec`; preserve the user's other worktrees and untracked files.
- The normative source is `docs/superpowers/specs/2026-08-21-pure-graph-engine-phase3-agent-runtime-adapters-design.md`; if this plan and the spec differ, stop and amend the plan before implementation.
- `graph-engine` contains no OpenCode, Cursor, agent-run, provider, prompt, persona, model, message, tool, session, or Assurance semantics.
- Do not modify `assurance_agent` or `assurance_kernel` runtime behavior, migrate Assurance capabilities, change `aa` composition, add an old-runtime bridge, or delete the old runtime.
- `TaskHandler.execute()` remains the normal dispatch interface. Recovery is an additive business-neutral protocol detected structurally at runtime.
- Plugin code never receives a ledger, store, lock, checkpoint, registry, unrestricted path, arbitrary event writer, execution transport selector, worker command, or codec.
- Every `execute`, `reconcile`, and `cancel` call crosses the fixed engine-owned `TaskExecutionHost`; no production in-process direct-call host is introduced.
- The exact host implementation/build digest, host wire-schema version, frozen capability entry point, request digest, workspace identity, and authorized secret handles are lock-bound.
- Credentials are resolved only through an ephemeral non-enumerable `SecretPort`; no secret value enters argv, lock, ledger, checkpoint, activity reference, receipt, result, digest input, or log.
- A recoverable attempt starts with one atomic durable batch: `TaskAttemptStarted`, `TaskLeaseAcquired`, and `TaskActivityPrepared`. No dispatch occurs before that batch is authoritative.
- A plugin may mark dispatch and bind one opaque reference through `TaskActivityPort`; it cannot publish terminal state. Only the engine promotes an authenticated final host receipt.
- Successful terminal observation requires host quiescence, an immutable sealed candidate, exact candidate tree ID, and write-set digest. Failed/stopped terminal observation carries no candidate.
- Recovery authenticates and reuses the original attempt workspace. It never calls `reset_attempt()`, creates a replacement attempt behind live work, or blindly retries an indeterminate dispatch.
- Reconcile precedes expired-lease reclaim. `running` adopts the same attempt; `terminal` promotes the same attempt; `not_dispatched` executes that same attempt once; `absent` requires strong proof; `indeterminate` blocks.
- OpenCode remains authoritative for sessions, messages, reasoning, tools, model history, token/cost data, and raw events. This repository adds no `SessionEvent` copy.
- OpenCode never supplies a caller-selected session ID and never treats `parentID` as reconnection. Ambiguous create is rediscovered by exact metadata and never issues a second create POST.
- Cursor uses no shell, runs in the exact attempt cwd with an allowlisted environment, and never adopts an unknown/dead/different-host process. A printed Cursor session identifier is evidence only.
- Every adapter forwards the exact frozen execution selection. No adapter chooses a model, agent, fallback, persona, prompt prefix, or provider default unless the request explicitly selects `provider_default`.
- Phase 3 is a hard cut for new Graph Engine runtime/checkpoint/plugin API versions. Phase 2 prototype invocation resume is unsupported and no compatibility alias is added.
- Use TDD for every task: establish RED, implement the smallest complete behavior, run focused GREEN, run the task regression gate, then commit one independently reviewable change.
- Before each task commit run `uv run pytest packages/graph-engine/tests -q` when Graph Engine changes, plus the affected new-wheel tests. Never commit a known regression.
- The frozen Phase 3 planning baseline is `992 passed, 1 skipped` for `packages/graph-engine/tests`; any later count change must be explained by added/removed tests, not silently accepted.
- Run all commands through `uv run`; Python is pinned to 3.11.

## File Responsibility Map

```text
packages/graph-engine/graph_engine/
  plugin_api.py                         generic request/context/activity/secret values and protocols
  composition/
    lock.py                             execution-host and wire-schema lock projection
    models.py                           FrozenComposition authentication of host selection
  runtime/
    activity.py                         engine-owned activity CAS port and recovery errors
    host_protocol.py                    fixed engine-owned host lifecycle transport values
    host_receipts.py                    immutable terminal receipt store/authentication
    events.py                           six generic activity/lease-adoption events
    models.py                           pure activity fold and projection invariants
    workspace.py                        attempt identity, reopen/authenticate, candidate sealing
    scheduler.py                        create-before-prepare, host dispatch, terminal promotion
    engine.py                           async recover-before-plan, cancel, adoption, finalization
    checkpoint.py                       schema bump; ledger-derived activity projection only

packages/agent-runtime-contracts/
  agent_runtime_contracts/models.py     frozen AgentRun request/result contracts
  agent_runtime_contracts/schema.py     strict result-schema validation/canonical encoding

packages/agent-runtime-opencode/
  agent_runtime_opencode/config.py      closed endpoint/profile/secret-handle configuration
  agent_runtime_opencode/protocol.py    typed HTTP/SSE projections and bounded client seam
  agent_runtime_opencode/discovery.py   exact metadata lookup/create reconciliation
  agent_runtime_opencode/handler.py     recoverable OpenCode lifecycle adapter
  agent_runtime_opencode/reducer.py     terminal result/evidence reduction
  agent_runtime_opencode/plugin.py      plugin descriptor and capability contribution

packages/agent-runtime-cursor/
  agent_runtime_cursor/config.py        pinned executable/profile/environment policy
  agent_runtime_cursor/process.py       confined process-host seam and opaque receipt
  agent_runtime_cursor/parser.py        strict bounded stream-json state machine
  agent_runtime_cursor/handler.py       recoverable Cursor lifecycle adapter
  agent_runtime_cursor/plugin.py        plugin descriptor and capability contribution

tests/agent_runtime/
  conformance.py                        shared black-box adapter contract
  fakes.py                              deterministic host/provider/process fault controls

examples/agent-runtime-fixture/
  agent_runtime_fixture/                neutral request assembler and strict result schema
  manifests/                            explicit OpenCode/Cursor one-task compositions

benchmark/agent-runtime-phase3/
  manifest.json                         pinned provider-live item inputs and expected evidence
  run-opencode.sh                       one real OpenCode release-evidence run
  run-cursor.sh                         one real Cursor release-evidence run
```

## Task Dependency Graph

```text
1 events/models ─> 2 fold ─> 4 activity port ─> 5 workspace ─> 6 recovery ─> 7 receipts/cancel
                         └─> 3 request/host/secret ─────────────┘
8 contracts ─> 9 OpenCode base ─> 10 discovery ─> 11 observation ─> 12 OpenCode closure
8 contracts ─> 13 Cursor base ─> 14 parser/terminal ─> 15 Cursor closure
7 + 12 + 15 ─> 16 shared conformance ─> 17 rebinding fixture ─> 18 release gates
```

---

### Task 1: Add generic activity values, events, errors, and canonical goldens

**Files:**
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Create: `packages/graph-engine/graph_engine/runtime/activity.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Modify: `packages/graph-engine/graph_engine/__init__.py`
- Create: `packages/graph-engine/tests/runtime/test_activity_models.py`
- Modify: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`
- Create: `packages/graph-engine/tests/runtime/activity-events-v2.golden.json`

**Interfaces:**
- Produces in `plugin_api.py`: `AttemptWorkspaceIdentity`, `TaskActivitySnapshot`, `TaskActivityReconcileResult`, and `TaskActivityCancelResult`; produces in the runtime: six activity events and six generic activity errors.
- Consumes: existing `FrozenModel`, `TaskOutcome`, canonical JSON/digest utilities, and `RuntimeEventModel`.
- Consumers: Tasks 2–7 and both adapter wheels.

- [ ] **Step 1: Write failing closed-model and canonical-event tests**

```python
def test_terminal_activity_requires_exact_success_candidate() -> None:
    outcome = TaskOutcome.succeeded({"answer": 42})
    with pytest.raises(ValueError, match="candidate tree and write-set"):
        TaskActivitySnapshot(
            activity_id="activity-1",
            request_digest="0" * 64,
            workspace_identity=_identity(),
            state="terminal_observed",
            terminal=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
        )


def test_activity_event_golden_is_canonical() -> None:
    events = _all_six_activity_events()
    assert canonical_json_bytes([event.model_dump(mode="json") for event in events]) == (
        GOLDEN.read_bytes()
    )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_models.py -v`

Expected: collection fails because the activity models and events do not exist.

- [ ] **Step 3: Implement exact frozen values and validators**

```python
ActivityState = Literal["prepared", "dispatch_started", "bound", "terminal_observed"]


class AttemptWorkspaceIdentity(FrozenModel):
    attempt_directory_id: str
    baseline_tree_id: str
    attempt_identity_digest: str
    layout_schema_version: Literal["1"] = "1"


class TaskActivitySnapshot(FrozenModel):
    activity_id: str
    request_digest: str
    workspace_identity: AttemptWorkspaceIdentity
    state: ActivityState
    reference: JSONValue | None = None
    reference_digest: str | None = None
    dispatch_fingerprint: JSONValue | None = None
    dispatch_fingerprint_digest: str | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    terminal: TaskOutcome | None = None
    outcome_digest: str | None = None
    terminal_proof_digest: str | None = None
    candidate_tree_id: str | None = None
    write_set_digest: str | None = None
```

Implement the frozen values and strict status-dependent validators in `plugin_api.py`, keeping them independent of runtime implementation modules. Put runtime errors and later CAS helpers in `runtime/activity.py`. Add `TaskActivityPrepared`, `TaskActivityDispatchStarted`, `TaskActivityBound`, `TaskActivityCancelRequested`, `TaskActivityTerminalObserved`, and `TaskLeaseAdopted` to the closed `RuntimeEvent` union.

- [ ] **Step 4: Regenerate and authenticate the v2 event golden**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_models.py packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -v`

Expected: all focused tests pass and byte-for-byte golden comparison is green.

- [ ] **Step 5: Run the task regression gate**

Run: `uv run pytest packages/graph-engine/tests/runtime -q`

Expected: runtime suite passes.

- [ ] **Step 6: Commit**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): define durable task activities"
```

---

### Task 2: Extend the pure fold with the activity state machine

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py`
- Create: `packages/graph-engine/tests/runtime/test_activity_fold.py`
- Modify: `packages/graph-engine/tests/runtime/test_planner.py`
- Modify: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`

**Interfaces:**
- Produces: `AttemptRecord.activity`, fold handlers for all six events, and planner rejection of a new attempt behind live/indeterminate activity.
- Consumes: Task 1 models/events and existing immutable `FoldCursor`.
- Consumer: scheduler/engine recovery in Tasks 4–7.

- [ ] **Step 1: Write failing legal-path and illegal-transition table tests**

```python
@pytest.mark.parametrize(
    "mutation, message",
    [
        ("success_without_bound", "success requires a bound activity"),
        ("duplicate_dispatch", "dispatch transition is already durable"),
        ("changed_reference", "activity reference is immutable"),
        ("failed_with_candidate", "failed terminal activity cannot have a candidate"),
        ("adopt_without_evidence", "lease adoption requires reconciliation evidence"),
    ],
)
def test_fold_rejects_illegal_activity_histories(mutation: str, message: str) -> None:
    envelopes = _mutated_history(mutation)
    with pytest.raises(ProjectionError, match=message):
        fold_events(envelopes)
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_fold.py -v`

Expected: failures show activity events are not projected or constrained.

- [ ] **Step 3: Add one immutable activity projection to each recoverable attempt**

```python
class AttemptRecord(ProjectionModel):
    attempt: int = Field(ge=1)
    lease_expires_at: str
    status: AttemptStatus = "running"
    activity: TaskActivitySnapshot | None = None
    # existing outcome, lease, and commit fields remain
```

Implement one event-dispatch branch per activity event in `_advance_fold()`. Each handler resolves the exact activation/attempt, validates identity and predecessor state, and returns a replaced immutable projection. Do not add provider-specific branches.

- [ ] **Step 4: Enforce terminal and attempt-outcome coupling**

Require recoverable `TaskAttemptSucceeded/Failed/Stopped` to follow one exact `TaskActivityTerminalObserved`. For success, require the attempt commit candidate to match `candidate_tree_id` and `write_set_digest`. Reject a new attempt while the prior activity is live or indeterminate.

- [ ] **Step 5: Prove fold purity and checkpoint deletion equivalence**

```python
def test_partitioned_fold_matches_one_shot_activity_fold() -> None:
    history = _complete_recoverable_history()
    first = FoldCursor.initial().advance(history[:7])
    assert first.advance(history[7:]).projection == fold_events(history)


def test_checkpoint_deletion_does_not_change_activity_projection(tmp_path: Path) -> None:
    ledger = _write_history(tmp_path, _bound_running_history())
    expected = fold_events(ledger.read_all())
    (tmp_path / "checkpoint.json").unlink(missing_ok=True)
    assert fold_events(ledger.read_all()) == expected
```

- [ ] **Step 6: Run focused and regression gates**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_fold.py packages/graph-engine/tests/runtime/test_planner.py packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -q`

Then: `uv run pytest packages/graph-engine/tests -q`

Expected: all Graph Engine tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): fold recoverable task activity state"
```

---

### Task 3: Finish request projection and define the fixed host/secret boundary

**Files:**
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Create: `packages/graph-engine/graph_engine/runtime/host_protocol.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/tests/composition/test_lock_model.py`
- Create: `packages/graph-engine/tests/runtime/test_host_protocol.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`

**Interfaces:**
- Produces: complete `TaskRequest`, `InvocationMetadata`, public `TaskActivityPort` protocol, `SecretPort`, expanded `TaskContext`, fixed `TaskExecutionHost.execute/reconcile/cancel`, `ExecutionHostLock`, and versioned host-call projections.
- Consumes: existing capability bindings/resources, Task 1 activity result values, and Phase 2 source/provenance lock.
- Consumers: Tasks 4–7 and adapter handlers.

- [ ] **Step 1: Write failing request-projection and secret-boundary tests**

```python
def test_scheduler_projects_exact_binding_resources_and_invocation_metadata() -> None:
    request = _captured_request_for_alias("fixture.agent.run")
    assert request.target_capability_id == "runtime.opencode.execute"
    assert request.binding_data == {"profile": "fixture-default"}
    assert request.resource_ids == ("fixture.instructions", "fixture.result-schema")
    assert request.resource_digests == {
        "fixture.instructions": _RESOURCE_DIGEST,
        "fixture.result-schema": _SCHEMA_DIGEST,
    }
    assert request.invocation.lock_digest == _LOCK_DIGEST


def test_secret_port_is_non_enumerable_and_rejects_unlocked_handle() -> None:
    port = _secret_port({"opencode.token": b"canary"})
    assert not hasattr(port, "keys")
    with pytest.raises(SecretHandleUnauthorized):
        port.resolve("cursor.token")
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/runtime/test_scheduler.py -q`

Expected: missing fields and lifecycle methods fail collection/assertions.

- [ ] **Step 3: Add the generic API shapes**

```python
class InvocationMetadata(FrozenModel):
    invocation_id: str
    lock_digest: str
    composition_digest: str
    entrypoint: str


class SecretPort(Protocol):
    def resolve(self, handle: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TaskContext:
    workspace_root: Path
    heartbeat: Callable[[], None]
    cancel_requested: Callable[[], bool]
    invocation: InvocationMetadata
    activity: TaskActivityPort | None = None
    secrets: SecretPort | None = None
```

Extend `TaskRequest` with canonical `resource_digests`, exact `ResourceClaims`, and immutable invocation metadata. Freeze every JSON field and validate unique sorted IDs.

- [ ] **Step 4: Define the fixed engine-owned lifecycle transport**

```python
class TaskExecutionHost(Protocol):
    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult: ...
    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult: ...
    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult: ...
    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]: ...
```

The call values carry only the exact resolved wheel entry point/capability, request, attempt-root descriptor capability, bounded activity RPC identity, authorized secret handles, host identity, and wire version. Do not expose a worker command or codec extension point.

- [ ] **Step 5: Pin host implementation and wire schema in `InvocationLock`**

```python
class ExecutionHostLock(FrozenModel):
    implementation_id: str
    implementation_digest: str
    wire_schema_version: Literal["1"]


class InvocationLock(FrozenModel):
    schema_version: Literal["2"] = "2"
    execution_host: ExecutionHostLock
    # existing source, registry, configuration, binding and workflow fields
```

Include the new projection in canonical bytes/digest and authenticate it in `FrozenComposition`.

- [ ] **Step 6: Run focused gates and update the lock golden**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/composition/test_lock_model.py -q`

Expected: exact request/host/secret/lock tests pass.

- [ ] **Step 7: Run the Graph Engine gate and commit**

Run: `uv run pytest packages/graph-engine/tests -q`

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests
git commit -m "feat(graph-engine): freeze task host execution boundary"
```

---

### Task 4: Implement the narrow task-activity CAS port

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/activity.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/ledger.py`
- Create: `packages/graph-engine/tests/runtime/test_activity_port.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`

**Interfaces:**
- Produces: the engine-owned `LedgerTaskActivityPort` implementation of `TaskActivityPort.snapshot`, `mark_dispatch_started()`, and `bind()`, bound to one exact invocation/task/activation/attempt/activity and expected ledger sequence.
- Consumes: Task 1 events/fold, Task 3 host RPC identity, ledger CAS/batch reconciliation.
- Consumers: all recoverable handlers and Task 6 recovery.

- [ ] **Step 1: Write failing idempotency, conflict, size, and stale-host tests**

```python
def test_exact_dispatch_repeat_does_not_append() -> None:
    port, ledger = _prepared_port()
    first = port.mark_dispatch_started({"endpoint": "https://localhost", "profile": "v1"})
    before = ledger.read_bytes()
    second = port.mark_dispatch_started({"endpoint": "https://localhost", "profile": "v1"})
    assert second == first
    assert ledger.read_bytes() == before


def test_stale_port_cannot_bind_another_attempt() -> None:
    port, _ = _prepared_port()
    _advance_attempt_elsewhere(port.activity_id)
    with pytest.raises(TaskActivityConflict):
        port.bind({"session_id": "ses_1"})
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_port.py -v`

Expected: missing port implementation and CAS behavior fail.

- [ ] **Step 3: Implement exact identity-bound append helpers**

```python
class LedgerTaskActivityPort:
    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._authenticate_current()

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        canonical = bounded_canonical_json(fingerprint, limit=_MAX_ACTIVITY_VALUE_BYTES)
        return self._append_or_authenticate(
            TaskActivityDispatchStarted(
                activity_id=self._identity.activity_id,
                ordinal=1,
                dispatch_fingerprint=canonical.value,
                dispatch_fingerprint_digest=canonical.digest,
            )
        )
```

`bind()` follows the same algorithm. Exact repeats return the folded snapshot without append; changed values raise typed drift/conflict. Reconcile ambiguous ledger-publication errors by authenticating the exact expected envelope range.

- [ ] **Step 4: Prove the port is not an arbitrary writer**

Assert the public object has no `append`, sequence setter, ledger property, cross-attempt selector, terminal method, or filesystem/store accessor.

- [ ] **Step 5: Run focused, race, and Graph Engine gates**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_port.py packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -q`

Then: `uv run pytest packages/graph-engine/tests -q`

- [ ] **Step 6: Commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): add identity-bound task activity port"
```

---

### Task 5: Create and authenticate the attempt workspace before preparation

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/workspace.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Create: `packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py`
- Modify: `packages/graph-engine/tests/runtime/test_workspace.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`

**Interfaces:**
- Produces: `SnapshotStore.create_attempt_identity()`, `SnapshotStore.open_attempt(identity)`, `AttemptWorkspaceLost`, orphan-before-prepare cleanup, and create-before-initial-batch ordering.
- Consumes: Task 1 `AttemptWorkspaceIdentity`, existing descriptor-relative store primitives, and Task 2 fold.
- Consumers: Tasks 6–7 recovery/terminal promotion.

- [ ] **Step 1: Write failing ordering and lost-workspace tests**

```python
def test_recoverable_start_creates_workspace_before_atomic_initial_batch() -> None:
    scheduler, ledger, store = _scheduler_with_boundaries()
    scheduler.start_recoverable(_task())
    assert store.boundaries.index("attempt_installed") < ledger.boundaries.index("batch_append")
    assert [event.kind for event in ledger.read_all()[-3:]] == [
        "task_attempt_started",
        "task_lease_acquired",
        "task_activity_prepared",
    ]


@pytest.mark.parametrize("mutation", ["missing", "replaced", "symlink", "baseline_drift"])
def test_recovery_never_recreates_lost_prepared_workspace(mutation: str) -> None:
    store, identity = _prepared_attempt_store()
    _mutate_attempt(store, identity, mutation)
    with pytest.raises(AttemptWorkspaceLost):
        store.open_attempt(identity)
    assert store.create_attempt_calls == 1
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py -v`

Expected: no durable attempt identity/open API and current start order does not satisfy the three-event batch.

- [ ] **Step 3: Add descriptor-relative identity construction and authentication**

Compute `attempt_directory_id` from the canonical invocation/task/activation/attempt tuple, bind it to authoritative baseline HEAD, and authenticate directory inode/type/link count/layout on every reopen. Return only the frozen identity; never serialize an unrestricted path.

```python
def create_attempt_identity(
    self,
    *,
    invocation_id: str,
    task_id: str,
    activation_id: str,
    attempt: int,
) -> tuple[AttemptWorkspace, AttemptWorkspaceIdentity]:
    attempt_directory_id = _attempt_directory_id(invocation_id, task_id, activation_id, attempt)
    workspace = self._create_attempt(attempt_directory_id)
    identity = workspace.snapshot_identity(
        invocation_id=invocation_id,
        task_id=task_id,
        activation_id=activation_id,
        attempt=attempt,
    )
    return workspace, identity


def open_attempt(self, identity: AttemptWorkspaceIdentity) -> AttemptWorkspace:
    workspace = self._open_attempt(identity.attempt_directory_id)
    workspace.authenticate_identity(identity)
    return workspace
```

- [ ] **Step 4: Reorder recoverable attempt startup**

For a recoverable handler: create/authenticate the workspace first; prospectively fold the three-event batch; append all three under one ledger CAS; only then construct the activity port and call the host. If the batch is proven absent, remove only the authenticated orphan. If publication is ambiguous, retain the workspace and reconcile the exact envelope range.

- [ ] **Step 5: Remove recovery dependence on `reset_attempt()`**

Add an exact source scan asserting activity recovery paths do not invoke `reset_attempt` or `create_attempts`. Keep disposable-handler behavior intact.

- [ ] **Step 6: Run fault and regression gates**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py packages/graph-engine/tests/runtime/test_workspace.py packages/graph-engine/tests/runtime/test_scheduler.py -q`

Then: `uv run pytest packages/graph-engine/tests -q`

- [ ] **Step 7: Commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): authenticate recoverable attempt workspaces"
```

---

### Task 6: Reconcile live activities before reclaim and adopt the same attempt

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/activity.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Create: `packages/graph-engine/tests/runtime/test_activity_recovery.py`
- Modify: `packages/graph-engine/tests/runtime/test_engine.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`

**Interfaces:**
- Produces: async `InvocationHandle.recover()`, `Scheduler.recover_activity()`, `TaskLeaseAdopted`, and typed recovery decisions.
- Consumes: Tasks 2–5 fold, host operations, activity port, and workspace identity.
- Consumer: Task 7 cancellation/terminal finalization.

- [ ] **Step 1: Write the failing recovery-order matrix**

```python
@pytest.mark.parametrize(
    "status, expected",
    [
        ("not_dispatched", "execute_same_attempt"),
        ("running", "adopt_same_attempt"),
        ("terminal", "promote_same_attempt"),
        ("absent", "finalize_failure_then_retry_policy"),
        ("indeterminate", "block"),
    ],
)
async def test_recovery_decision_matrix(status: str, expected: str) -> None:
    fixture = await _crashed_recoverable_attempt(status)
    result = await fixture.handle.recover()
    assert fixture.calls.order[:2] == ["authenticate_workspace", "reconcile"]
    assert fixture.observed_decision(result) == expected
```

Add an expired-lease test asserting `reconcile` happens before any reclaim/adoption event and that `attempt` remains unchanged.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py -v`

Expected: current `Engine.open()` reclaim path appends failure before provider reconciliation.

- [ ] **Step 3: Separate synchronous open from asynchronous recovery**

`Engine.open()` authenticates lock, intent, ledger, checkpoint, HEAD, activity fold, and workspace identity only. It does not reclaim recoverable activities. Add:

```python
class InvocationHandle:
    async def recover(self) -> RecoveryResult:
        return await self._engine._recover_invocation(self)
```

Invoke this before `plan_next()`/the next scheduler wave. Preserve synchronous replay for already-terminal invocations.

- [ ] **Step 4: Implement the exact decision order**

For each live recoverable activity in canonical task order: authenticate original workspace; check ledger terminal observation; check host terminal receipt; run durable cancel first when requested; reconcile; bind a unique returned reference; then map the closed status. Exceptions and malformed values become protocol violation/indeterminate, never absence.

- [ ] **Step 5: Append same-attempt adoption atomically**

```python
TaskLeaseAdopted(
    activity_id=activity.activity_id,
    task_id=task.task_id,
    activation_id=task.activation_id,
    attempt=task.attempt,
    owner_id=self._owner_id,
    acquired_at=now,
    heartbeat_at=now,
    expires_at=now + self._lease_seconds,
    reconciliation_evidence_digest=evidence_digest,
)
```

Validate/fold/CAS the event before continuing. Do not emit a new `TaskAttemptStarted`.

- [ ] **Step 6: Prove no blind retry**

Assert provider timeout, empty ambiguous discovery, missing formerly bound activity, process disappearance, malformed response, and `indeterminate` leave ledger/workspace/attempt number unchanged and prevent planning a new attempt.

- [ ] **Step 7: Run recovery and full Graph Engine gates**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_scheduler.py -q`

Then: `uv run pytest packages/graph-engine/tests -q`

- [ ] **Step 8: Commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): reconcile activities before lease reclaim"
```

---

### Task 7: Add cancellation, durable host receipts, quiescence, and terminal promotion

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/host_receipts.py`
- Modify: `packages/graph-engine/graph_engine/runtime/host_protocol.py`
- Modify: `packages/graph-engine/graph_engine/runtime/activity.py`
- Modify: `packages/graph-engine/graph_engine/runtime/workspace.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Create: `packages/graph-engine/tests/runtime/test_host_receipts.py`
- Create: `packages/graph-engine/tests/runtime/test_activity_cancellation.py`
- Modify: `packages/graph-engine/tests/runtime/test_activity_recovery.py`

**Interfaces:**
- Produces: `TaskHostTerminalReceipt`, capability-limited `TerminalReceiptSink`, immutable receipt store, cancel-request path, host quiescence proof, success candidate sealing, terminal promotion, receipt cleanup, and bounded cancel timeout.
- Consumes: Tasks 1–6 generic runtime state only.
- Consumers: both adapters and shared conformance.

- [ ] **Step 1: Write the terminal-receipt crash matrix RED**

```python
@pytest.mark.parametrize(
    "cut",
    [
        "before_quiescence",
        "after_quiescence_before_receipt",
        "after_receipt_fsync",
        "after_receipt_rename",
        "after_candidate_seal",
        "after_terminal_event_install",
        "after_terminal_event_fsync",
        "before_receipt_cleanup",
    ],
)
async def test_terminal_receipt_cut_recovery_is_exactly_once(cut: str) -> None:
    fixture = await _cut_terminal_call(cut)
    recovered = await fixture.reopen_and_recover()
    assert recovered.provider_calls <= 1
    assert recovered.terminal_events == 1
    assert recovered.candidate_matches_receipt
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_host_receipts.py packages/graph-engine/tests/runtime/test_activity_cancellation.py -v`

Expected: receipt/cancel APIs are absent.

- [ ] **Step 3: Implement the exact immutable receipt**

```python
class TaskHostTerminalReceipt(FrozenModel):
    schema_version: Literal["1"] = "1"
    host_implementation_digest: str
    wire_schema_version: Literal["1"]
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int
    activity_id: str
    operation: Literal["execute", "reconcile", "cancel"]
    request_digest: str
    workspace_identity_digest: str
    dispatch_fingerprint_digest: str | None
    reference_digest: str | None
    outcome: TaskOutcome
    outcome_digest: str
    terminal_proof_digest: str | None
    quiescence_proof_digest: str
    host_call_id: int
```

Install it through a host-call-scoped `TerminalReceiptSink` that can address only the exact call identity; it exposes no parent directory, arbitrary filename, read API, or delete operation. The sink performs descriptor-relative temp write, file fsync, no-replace rename, and receipt-directory fsync. Reject partial, symlink, linked, changed, multiple, foreign, or non-monotonic receipts. No prepare receipt is promotable.

- [ ] **Step 4: Make terminal acknowledgement receipt-first**

The host must end the handler call, prove every writer/descendant quiescent, install the final receipt, and only then return. Engine recovery always checks that receipt before invoking adapter reconciliation/cancellation.

- [ ] **Step 5: Seal success before terminal event**

Add a workspace method that authenticates the original attempt and creates an immutable candidate without making it HEAD. Validate declared writes and commit validators, then append one `TaskActivityTerminalObserved` containing exact outcome/candidate/write-set digests. Failed/stopped outcomes append without candidate. Delete the receipt only after the exact event is authoritative.

- [ ] **Step 6: Add durable cancellation**

Append `TaskActivityCancelRequested` before the host cancel call. Map `acknowledged` to continued reconciliation, `terminal` through receipt/candidate promotion, and `indeterminate`/timeout/exception to a preserved blocking activity. Sending a signal or receiving HTTP abort success is never terminal by itself.

- [ ] **Step 7: Prove effects/checkpoints cannot authorize activity**

Delete checkpoints and inject effect events around live activities; assert identical provider call count and recovery decision. Reject any attempt to encode create/bind/cancel/terminal state as an effect.

- [ ] **Step 8: Run focused, fault, and Graph Engine gates**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_host_receipts.py packages/graph-engine/tests/runtime/test_activity_cancellation.py packages/graph-engine/tests/runtime/test_activity_recovery.py -q`

Then: `uv run pytest packages/graph-engine/tests -q`

- [ ] **Step 9: Commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): promote quiescent task terminal receipts"
```

---

### Task 8: Create the frozen provider-neutral AgentRun contracts wheel

**Files:**
- Create: `packages/agent-runtime-contracts/pyproject.toml`
- Create: `packages/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Create: `packages/agent-runtime-contracts/agent_runtime_contracts/models.py`
- Create: `packages/agent-runtime-contracts/agent_runtime_contracts/schema.py`
- Create: `packages/agent-runtime-contracts/tests/test_models.py`
- Create: `packages/agent-runtime-contracts/tests/test_schema.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `InstructionPart`, `ResultContract`, `FrozenExecutionSelection`, `AgentRunRequest`, `AgentRunResult`, canonical bytes/digests, and strict result validation.
- Consumes: public frozen JSON/value primitives from `graph_engine.plugin_api`; imports no Graph Engine runtime/composition implementation and no adapter package.
- Consumers: both adapter wheels and the neutral fixture.

- [ ] **Step 1: Write failing strict/frozen/canonical tests**

```python
def test_agent_run_request_is_strict_frozen_and_canonical() -> None:
    request = AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest="1" * 64,
            extraction_mode="structured",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest="2" * 64,
            limits={"max_seconds": 120},
        ),
        request_policy_digest="3" * 64,
        request_config_digest="4" * 64,
    )
    assert AgentRunRequest.model_validate_json(request.canonical_bytes()).canonical_bytes() == (
        request.canonical_bytes()
    )
    with pytest.raises(ValidationError, match="extra"):
        AgentRunRequest.model_validate({**request.model_dump(), "fallback_model": "x"})
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-contracts/tests -v`

Expected: the package and models do not exist.

- [ ] **Step 3: Implement the exact request values**

Use `ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)`. `InstructionPart` permits only a closed media type and exactly one text or canonical JSON content form, plus its verified digest. `FrozenExecutionSelection` contains one exact selection or explicit `provider_default`; reject candidate lists, routing expressions, fallbacks, and unknown fields.

- [ ] **Step 4: Implement the result contract**

```python
class AgentRunResult(FrozenModel):
    schema_version: Literal["1"] = "1"
    structured_result: JSONValue
    result_digest: str
    evidence_digest: str
    provider_diff_digest: str | None = None
    adapter_id: str
    adapter_version: str
    diagnostics: tuple[str, ...] = ()
```

Validate structured output against the exact strict schema, authenticate every digest, bound and redact diagnostics, and forbid engine candidate IDs, provider transcripts, model histories, tokens, costs, raw events, and secrets.

- [ ] **Step 5: Add workspace metadata and isolation tests**

Add `agent-runtime-contracts` to uv workspace/dev dependencies and Pyright paths. Build/install only Graph Engine plus this contract wheel in an empty venv and assert importing it does not import either adapter or any Assurance package; an import of the public `graph_engine.plugin_api` value surface is permitted and mechanically constrained.

- [ ] **Step 6: Run wheel gates and commit**

Run: `uv run pytest packages/agent-runtime-contracts/tests -q`

Run: `uv run ruff check packages/agent-runtime-contracts && uv run pyright packages/agent-runtime-contracts`

```bash
git add pyproject.toml uv.lock packages/agent-runtime-contracts
git commit -m "feat(agent-runtime): define frozen run contracts"
```

---

### Task 9: Create the OpenCode adapter wheel, closed configuration, preflight, and HTTP fake

**Files:**
- Create: `packages/agent-runtime-opencode/pyproject.toml`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/__init__.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/config.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/protocol.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/plugin.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/plugin-declaration.json`
- Create: `packages/agent-runtime-opencode/tests/fake_server.py`
- Create: `packages/agent-runtime-opencode/tests/test_config.py`
- Create: `packages/agent-runtime-opencode/tests/test_preflight.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: closed `OpenCodeAdapterConfig`, `OpenCodeProtocolProfile`, typed bounded client seam, fake server fault controls, plugin descriptor, and preflight fingerprint.
- Consumes: `agent-runtime-contracts` and public Graph Engine plugin API only.
- Consumers: Tasks 10–12.

- [ ] **Step 1: Write failing closed-config and exact-preflight tests**

```python
def test_opencode_config_rejects_ambient_auth_and_fallbacks() -> None:
    with pytest.raises(ValidationError):
        OpenCodeAdapterConfig.model_validate(
            {
                "endpoint": "http://127.0.0.1:4096",
                "secret_handle": "opencode.token",
                "profile": "opencode-http-v1",
                "model_fallback": "auto",
            }
        )


async def test_preflight_authenticates_idempotent_prompt_profile() -> None:
    fake = OpenCodeFakeServer(profile=_profile(prompt_idempotency="conflict-on-body-drift"))
    fingerprint = await OpenCodeHandler(_config(fake)).preflight(_request(), _context())
    assert fingerprint["prompt_admission"] == "caller-message-id-v1"
    assert "secret" not in canonical_json_text(fingerprint)
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_config.py packages/agent-runtime-opencode/tests/test_preflight.py -v`

Expected: OpenCode wheel/config/fake are absent.

- [ ] **Step 3: Implement the closed profile and configuration**

```python
class OpenCodeAdapterConfig(FrozenModel):
    schema_version: Literal["1"] = "1"
    endpoint: AnyHttpUrl
    tls_identity_digest: str
    secret_handle: str
    protocol_profile: str
    project_scope: str
    request_timeout_seconds: float = Field(gt=0, le=300)
    observation_horizon_seconds: float = Field(gt=0, le=3600)
    max_response_bytes: int = Field(gt=0, le=4_000_000)
```

Reject URL credentials, redirects to a different origin, undeclared secret handles, provider/model defaults, unbounded response values, and unknown fields. Canonical config contains only the secret handle, never secret bytes.

- [ ] **Step 4: Define a bounded typed client seam and deterministic fake**

The client exposes only the pinned routes used by the profile: server/profile identity, scoped session list/create/get, exact message admission/get, status, SSE, and abort. Every response passes byte/time/shape limits before parsing. The fake records request method/path/body digest and supports cut injection before request, after provider mutation, before response, malformed response, SSE gap, and polling lag.

- [ ] **Step 5: Add the plugin entry point without a default product**

Register one explicit capability such as `runtime.opencode.execute` and its request/result schemas. The descriptor declares exact source/version/config/resource IDs. Do not register a graph, product, alias, prompt, model, or default endpoint.

- [ ] **Step 6: Run package/import gates and commit**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_config.py packages/agent-runtime-opencode/tests/test_preflight.py -q`

Run: `uv run ruff check packages/agent-runtime-opencode && uv run pyright packages/agent-runtime-opencode`

```bash
git add pyproject.toml uv.lock packages/agent-runtime-opencode
git commit -m "feat(agent-runtime-opencode): add closed adapter profile"
```

---

### Task 10: Implement OpenCode metadata discovery, create ambiguity, and reference binding

**Files:**
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/discovery.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Create: `packages/agent-runtime-opencode/tests/test_discovery.py`
- Create: `packages/agent-runtime-opencode/tests/test_create_recovery.py`
- Modify: `packages/agent-runtime-opencode/tests/fake_server.py`

**Interfaces:**
- Produces: canonical discovery metadata, opaque bound reference, exact pre-create lookup, one-create rule, and ambiguous-create rediscovery.
- Consumes: Task 4 activity port and Task 9 typed client/config.
- Consumers: Task 11 prompt/observation and Task 12 terminal reduction.

- [ ] **Step 1: Write the generated-ID and ambiguous-create RED matrix**

```python
@pytest.mark.parametrize(
    "cut",
    ["before_create", "after_create_before_response", "invalid_success_body", "proxy_reset"],
)
async def test_ambiguous_create_never_posts_twice(cut: str) -> None:
    fixture = _open_code_fixture(create_cut=cut)
    await fixture.execute_until_cut()
    await fixture.reconcile_twice()
    assert fixture.fake.count("POST", "/session") <= 1


async def test_multiple_exact_metadata_matches_fail_closed() -> None:
    fixture = _open_code_fixture(existing_matches=2)
    result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
    assert result.status == "indeterminate"
    assert fixture.fake.create_calls == 0
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_discovery.py packages/agent-runtime-opencode/tests/test_create_recovery.py -v`

Expected: discovery/handler modules do not exist.

- [ ] **Step 3: Define canonical metadata and opaque reference**

```python
class OpenCodeDiscoveryMetadata(FrozenModel):
    schema_version: Literal["1"] = "1"
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int
    activity_id: str
    request_digest: str
    workspace_identity_digest: str
    adapter_source_digest: str


class OpenCodeActivityReference(FrozenModel):
    profile_identity_digest: str
    session_id: str | None
    metadata_match_digest: str
    request_digest: str
    expected_message_id: str
    prompt_body_digest: str
    adapter_version: str
```

The adapter never supplies a session ID to create and never stores secret material in either value.

- [ ] **Step 4: Implement the ordered create/bind algorithm**

Validate request/workspace; call `mark_dispatch_started()` with observed endpoint/profile identity; list exact scope and compare metadata canonically; bind exactly one match; fail closed on multiple; on none send one metadata-bearing create POST; validate and bind returned ID before prompt; on ambiguous response return `indeterminate` and rediscover without another POST.

- [ ] **Step 5: Enforce empty-list and foreign/drift semantics**

After ambiguous create, an empty list remains indeterminate until the bounded observation horizon; it never becomes `absent`. A formerly bound 404 is missing/indeterminate, not create authorization. Reject foreign metadata, changed endpoint/profile/request/message identity, and any `parentID` reconnect attempt.

- [ ] **Step 6: Run focused and adapter gates**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_discovery.py packages/agent-runtime-opencode/tests/test_create_recovery.py -q`

Then: `uv run pytest packages/agent-runtime-opencode/tests -q`

- [ ] **Step 7: Commit**

```bash
git add packages/agent-runtime-opencode
git commit -m "feat(agent-runtime-opencode): reconcile generated session identity"
```

---

### Task 11: Add exact prompt admission, SSE-primary observation, polling fallback, and cancellation

**Files:**
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Create: `packages/agent-runtime-opencode/tests/test_prompt_admission.py`
- Create: `packages/agent-runtime-opencode/tests/test_observation.py`
- Create: `packages/agent-runtime-opencode/tests/test_cancel.py`
- Modify: `packages/agent-runtime-opencode/tests/fake_server.py`

**Interfaces:**
- Produces: deterministic message admission, SSE/GET state reduction, bounded polling fallback, heartbeat integration, and acknowledged-only abort semantics.
- Consumes: bound reference from Task 10 and exact frozen `AgentRunRequest`.
- Consumer: Task 12 terminal reducer/fault closure.

- [ ] **Step 1: Write failing prompt idempotency crash tests**

```python
@pytest.mark.parametrize(
    "cut",
    ["before_prompt_post", "after_admission_before_response", "after_lost_success_response"],
)
async def test_prompt_crashes_converge_to_one_admission(cut: str) -> None:
    fixture = _bound_fixture(prompt_cut=cut)
    await fixture.run_and_reconcile()
    assert fixture.fake.accepted_message_count(fixture.reference.expected_message_id) == 1


async def test_same_message_id_with_changed_body_fails_closed() -> None:
    fixture = _bound_fixture(existing_message_body={"changed": True})
    result = await fixture.reconcile()
    assert result.status == "indeterminate"
    assert fixture.fake.prompt_posts == 0
```

- [ ] **Step 2: Write failing SSE/poll/cancel tests and confirm RED**

Test fast terminal transition, SSE gap plus exact GET, cursor reconnect, silent SSE fallback, status-map omission, transient idle, open tool work, completion/cancel race, and abort-200 acknowledgement.

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_prompt_admission.py packages/agent-runtime-opencode/tests/test_observation.py packages/agent-runtime-opencode/tests/test_cancel.py -v`

- [ ] **Step 3: Implement exact prompt construction and admission**

Construct the body only from canonical `AgentRunRequest`; forward the exact selected provider/model/profile fields and no extras. Bind deterministic message ID and body/delivery digest before the first prompt POST. On recovery, read exact admission; exact reuse may repeat the same idempotent call, conflict/duplicate fails closed, and an absent read never permits a new ID/body.

- [ ] **Step 4: Implement SSE-primary observation**

Parse only pinned envelope fields; validate exact session identity; heartbeat on valid progress/keepalive; reject malformed identity-bearing events; reconnect using the locked cursor when supported; authenticate state with GET after any gap; fall back to bounded polling when the locked profile permits it.

- [ ] **Step 5: Implement authoritative GET terminal checks**

Absence from the aggregate status map triggers exact session GET plus messages/tool-state checks. One idle observation is insufficient. Success requires terminal/idle state plus complete final structured result and no open tool work. Error/cancel requires its terminal provider record.

- [ ] **Step 6: Implement acknowledged-only cancellation**

Validate the bound reference, issue the locked idempotent abort at most as configured, return `acknowledged` for HTTP success, and continue reconciliation. If completion races cancel, one authenticated provider terminal result wins. Missing/foreign/drift remains indeterminate.

- [ ] **Step 7: Run the adapter gate and commit**

Run: `uv run pytest packages/agent-runtime-opencode/tests -q`

Run: `uv run ruff check packages/agent-runtime-opencode && uv run pyright packages/agent-runtime-opencode`

```bash
git add packages/agent-runtime-opencode
git commit -m "feat(agent-runtime-opencode): observe idempotent session execution"
```

---

### Task 12: Reduce OpenCode terminal state and close the full fault/security matrix

**Files:**
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/reducer.py`
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Create: `packages/agent-runtime-opencode/agent_runtime_opencode/redaction.py`
- Create: `packages/agent-runtime-opencode/tests/test_terminal_reduction.py`
- Create: `packages/agent-runtime-opencode/tests/test_fault_matrix.py`
- Create: `packages/agent-runtime-opencode/tests/test_credentials.py`
- Modify: `packages/agent-runtime-opencode/tests/fake_server.py`

**Interfaces:**
- Produces: schema-valid `AgentRunResult`, typed `TaskOutcome`, provider diff/evidence digests, bounded diagnostics, credential redaction, and complete OpenCode fault evidence.
- Consumes: Tasks 8–11.
- Consumer: shared conformance and fixture.

- [ ] **Step 1: Write failing terminal reduction and retention-boundary tests**

```python
async def test_terminal_reduction_keeps_full_session_state_out_of_result() -> None:
    outcome = await _terminal_success_outcome()
    result = AgentRunResult.model_validate(outcome.output)
    encoded = result.model_dump_json()
    for forbidden in ("messages", "reasoning", "tool_calls", "model_history", "token_count", "cost"):
        assert forbidden not in encoded


def test_post_success_replay_needs_no_opencode_access() -> None:
    fixture = _completed_engine_invocation()
    fixture.fake.reject_all_requests()
    assert fixture.reopen_and_run().terminal == "succeeded"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_terminal_reduction.py packages/agent-runtime-opencode/tests/test_fault_matrix.py packages/agent-runtime-opencode/tests/test_credentials.py -v`

- [ ] **Step 3: Implement strict terminal reduction**

Fetch only structured result, terminal/error state, supported diff, and canonical bounded history/tool evidence digests. Validate the exact `ResultContract`, construct `AgentRunResult`, and return `TaskOutcome.succeeded()` or an explicitly retryable/non-retryable typed failure. Do not persist full remote bodies.

- [ ] **Step 4: Centralize redaction before size limiting or digesting**

Redact authorization headers, cookies, tokens, API keys, secret environment values, URL credentials, and configured canaries before diagnostics are truncated, logged, or hashed. Scan all durable engine files and captured logs for byte and encoded forms of each canary.

- [ ] **Step 5: Complete the deterministic fault matrix**

Cover create/prompt/SSE/poll/GET/cancel cuts, temporary empty discovery, multiple matches, missing bound session, foreign/drift, malformed/oversized responses, result-schema failure, fast terminal, lost SSE, idle-with-open-tools, completion/cancel race, receipt-before-engine-ack, and replay without provider.

- [ ] **Step 6: Run adapter, Graph Engine, and static gates**

Run: `uv run pytest packages/agent-runtime-opencode/tests packages/graph-engine/tests -q`

Run: `uv run ruff check packages/agent-runtime-opencode packages/graph-engine && uv run pyright packages/agent-runtime-opencode packages/graph-engine`

- [ ] **Step 7: Commit**

```bash
git add packages/agent-runtime-opencode
git commit -m "feat(agent-runtime-opencode): close terminal recovery evidence"
```

---

### Task 13: Create the Cursor adapter wheel and confined process-host interface

**Files:**
- Create: `packages/agent-runtime-cursor/pyproject.toml`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/__init__.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/config.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/plugin.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/plugin-declaration.json`
- Create: `packages/agent-runtime-cursor/tests/fake_process_host.py`
- Create: `packages/agent-runtime-cursor/tests/test_config.py`
- Create: `packages/agent-runtime-cursor/tests/test_process_launch.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: closed Cursor configuration, pinned executable preflight, confined process-host seam, launch fingerprint, and plugin descriptor.
- Consumes: public Graph Engine plugin API and `agent-runtime-contracts` only.
- Consumers: Tasks 14–15.

- [ ] **Step 1: Write failing no-shell/confinement/environment tests**

```python
async def test_cursor_launch_is_exact_and_shell_free(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost()
    await CursorHandler(_config(), host).execute(_request(), _context(tmp_path))
    launch = host.launches[0]
    assert launch.argv[:3] == (_CURSOR_BIN, "agent", "--print")
    assert launch.shell is False
    assert launch.cwd == tmp_path.resolve()
    assert set(launch.environment) == {"PATH", "CURSOR_API_KEY"}


async def test_launch_rejects_host_without_descendant_confinement() -> None:
    with pytest.raises(TaskActivityProtocolViolation, match="confinement"):
        await CursorHandler(_config(), FakeConfinedProcessHost(available=False)).execute(
            _request(), _context()
        )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_config.py packages/agent-runtime-cursor/tests/test_process_launch.py -v`

Expected: Cursor package/config/process host do not exist.

- [ ] **Step 3: Implement closed configuration and executable authentication**

```python
class CursorAdapterConfig(FrozenModel):
    schema_version: Literal["1"] = "1"
    executable: str
    executable_digest: str
    expected_version: str
    secret_handle: str | None = None
    environment_names: tuple[str, ...]
    graceful_cancel_seconds: float = Field(gt=0, le=60)
    forced_cancel_seconds: float = Field(gt=0, le=60)
    max_output_bytes: int = Field(gt=0, le=16_000_000)
    max_line_bytes: int = Field(gt=0, le=1_000_000)
```

Before dispatch, authenticate regular executable identity, binary digest, reported version, exact profile, and supported frozen execution selection. Reject shell fragments, relative executable search, ambient environment inheritance, unsupported selection fields, and unknown keys.

- [ ] **Step 4: Define the mandatory confinement seam**

```python
class ConfinedProcessHost(Protocol):
    def preflight(self, request: ProcessLaunchRequest) -> ConfinementIdentity: ...
    async def spawn(self, request: ProcessLaunchRequest) -> ConfinedProcess: ...
    async def observe(self, receipt: CursorProcessReceipt) -> ProcessObservation: ...
    async def terminate(self, receipt: CursorProcessReceipt, policy: CancelPolicy) -> None: ...
```

The host must prove authenticated process-group/job/cgroup/container identity and descendant inheritance before plugin execution. A PID plus `finally: kill()` is insufficient and must fail preflight.

- [ ] **Step 5: Construct exact argv/stdin/cwd/environment**

Pass the canonical request through the documented stdin/request path, never shell interpolation. Use only exact attempt workspace cwd and allowlisted environment entries resolved from authorized secret handles. Record non-secret argv-policy and executable/version digests in the dispatch fingerprint.

- [ ] **Step 6: Run package/static gates and commit**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_config.py packages/agent-runtime-cursor/tests/test_process_launch.py -q`

Run: `uv run ruff check packages/agent-runtime-cursor && uv run pyright packages/agent-runtime-cursor`

```bash
git add pyproject.toml uv.lock packages/agent-runtime-cursor
git commit -m "feat(agent-runtime-cursor): add confined process adapter"
```

---

### Task 14: Add Cursor process receipts, strict stream parsing, terminal reduction, and cancellation

**Files:**
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/parser.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/handler.py`
- Create: `packages/agent-runtime-cursor/tests/test_process_receipt.py`
- Create: `packages/agent-runtime-cursor/tests/test_stream_parser.py`
- Create: `packages/agent-runtime-cursor/tests/test_terminal_reduction.py`
- Create: `packages/agent-runtime-cursor/tests/test_cancel.py`
- Modify: `packages/agent-runtime-cursor/tests/fake_process_host.py`

**Interfaces:**
- Produces: opaque `CursorProcessReceipt`, bounded strict parser, `AgentRunResult` reduction, host terminal result, and acknowledged-only process cancellation.
- Consumes: Task 13 authenticated launch and Task 8 contracts.
- Consumer: Task 15 recovery/fault closure.

- [ ] **Step 1: Write failing process-receipt and parser matrices**

```python
def test_process_receipt_binds_non_reusable_identity() -> None:
    receipt = _spawned_receipt()
    assert receipt.host_boot_identity_digest == _BOOT_DIGEST
    assert receipt.confinement_identity
    assert receipt.process_start_token
    assert receipt.workspace_identity_digest == _WORKSPACE_DIGEST
    assert "api-key" not in receipt.model_dump_json()


@pytest.mark.parametrize(
    "fixture, message",
    [
        ("missing_init", "one system init"),
        ("changed_session", "session identity changed"),
        ("oversized_line", "line byte limit"),
        ("missing_terminal", "terminal record"),
        ("exit_mismatch", "exit status"),
    ],
)
def test_stream_parser_fails_closed(fixture: str, message: str) -> None:
    with pytest.raises(CursorProtocolError, match=message):
        parse_stream(_stream_fixture(fixture), _limits())
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_process_receipt.py packages/agent-runtime-cursor/tests/test_stream_parser.py -v`

- [ ] **Step 3: Bind the opaque process receipt immediately after spawn**

```python
class CursorProcessReceipt(FrozenModel):
    host_boot_identity_digest: str
    confinement_identity: str
    process_group_identity: str
    process_start_token: str
    executable_version_digest: str
    request_digest: str
    argv_policy_digest: str
    workspace_identity_digest: str
    started_at: float
    stream_session_id: str | None = None
```

Bind it through `TaskActivityPort` before consuming provider progress. A stream session ID may extend evidence but never authorizes `--resume` or unknown-process adoption.

- [ ] **Step 4: Implement a bounded newline-JSON state machine**

Require exactly one compatible init, exact cwd, one consistent session ID, known structural event types with documented additive-field tolerance, one terminal result, and exit/terminal consistency. Enforce total bytes, line bytes, record count, JSON nesting, stderr, and elapsed-time limits before retention.

- [ ] **Step 5: Reduce terminal state to the shared contract**

Validate the terminal structured result against `ResultContract`; compute result/process/evidence digests; return `TaskOutcome.succeeded(AgentRunResult)` or an explicit terminal failure. Keep assistant deltas, tool records, raw stream, and stderr transient.

- [ ] **Step 6: Implement cancellation without false terminal claims**

Authenticate receipt/host identity; send graceful group signal; wait bounded interval; invoke host containment forced termination when policy permits. Signal delivery is `acknowledged` only. Return terminal only with complete authenticated terminal stream plus exit.

- [ ] **Step 7: Run focused and adapter gates**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_process_receipt.py packages/agent-runtime-cursor/tests/test_stream_parser.py packages/agent-runtime-cursor/tests/test_terminal_reduction.py packages/agent-runtime-cursor/tests/test_cancel.py -q`

Then: `uv run pytest packages/agent-runtime-cursor/tests -q`

- [ ] **Step 8: Commit**

```bash
git add packages/agent-runtime-cursor
git commit -m "feat(agent-runtime-cursor): authenticate stream terminal state"
```

---

### Task 15: Close Cursor crash, unknown-process, cancellation, and credential fault semantics

**Files:**
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/handler.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/redaction.py`
- Create: `packages/agent-runtime-cursor/tests/test_recovery.py`
- Create: `packages/agent-runtime-cursor/tests/test_fault_matrix.py`
- Create: `packages/agent-runtime-cursor/tests/test_credentials.py`
- Modify: `packages/agent-runtime-cursor/tests/fake_process_host.py`

**Interfaces:**
- Produces: honest `not_dispatched/running/terminal/indeterminate` reconciliation, receipt-first promotion, unknown-host rejection, and complete Cursor fault/security evidence.
- Consumes: Tasks 7, 13, and 14.
- Consumer: shared conformance and neutral fixture.

- [ ] **Step 1: Write the failing crash/recovery matrix**

```python
@pytest.mark.parametrize(
    "cut, expected",
    [
        ("before_spawn", "not_dispatched"),
        ("after_spawn_before_bind", "indeterminate"),
        ("after_bind", "running"),
        ("mid_stream", "indeterminate"),
        ("after_host_terminal_receipt", "terminal"),
        ("after_process_exit_without_terminal", "indeterminate"),
    ],
)
async def test_cursor_recovery_never_blindly_retries(cut: str, expected: str) -> None:
    fixture = await _cursor_cut(cut)
    result = await fixture.reconcile_after_restart()
    assert result.status == expected
    if expected == "indeterminate":
        assert fixture.spawn_count == 1
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_recovery.py packages/agent-runtime-cursor/tests/test_fault_matrix.py -v`

- [ ] **Step 3: Implement honest recovery capability**

Return `not_dispatched` only when no spawn could have occurred. Observe a child only when the same live authenticated host owns the exact boot/confinement/start-token receipt. Promote a valid durable host terminal receipt without Cursor access. Dead/different host, PID reuse, missing child, truncated stream, unknown exit, or receipt drift returns `indeterminate`, never `absent`.

- [ ] **Step 4: Complete host and cancellation race tests**

Cover host boot change, PID reuse, confinement unavailable, cleanup success/ambiguity, graceful/forced cancellation, completion/cancel race, unknown exit, terminal receipt surviving engine crash, and provider session ID printed but never used for adoption.

- [ ] **Step 5: Add credential canary scanning**

Assert secrets are absent from argv, process receipt, activity reference, host receipt, result, evidence digest inputs, logs, stderr projection, every engine durable file, and exception strings. Confirm only the authorized environment name receives bytes during the confined call and is revoked afterward.

- [ ] **Step 6: Run adapter, Graph Engine, and static gates**

Run: `uv run pytest packages/agent-runtime-cursor/tests packages/graph-engine/tests -q`

Run: `uv run ruff check packages/agent-runtime-cursor packages/graph-engine && uv run pyright packages/agent-runtime-cursor packages/graph-engine`

- [ ] **Step 7: Commit**

```bash
git add packages/agent-runtime-cursor
git commit -m "feat(agent-runtime-cursor): fail closed on unknown process state"
```

---

### Task 16: Add shared adapter conformance with honest capability assertions

**Files:**
- Create: `tests/agent_runtime/__init__.py`
- Create: `tests/agent_runtime/conformance.py`
- Create: `tests/agent_runtime/fakes.py`
- Create: `tests/agent_runtime/test_opencode_conformance.py`
- Create: `tests/agent_runtime/test_cursor_conformance.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: one black-box `RuntimeAdapterHarness` and common tests for request fidelity, prepare/dispatch/bind/cancel/terminal ordering, host confinement, receipt promotion, workspace preservation, secrets, and recovery outcomes.
- Consumes: public Graph Engine and contract APIs plus each adapter's public handler/config; no adapter-private imports in common assertions.
- Consumer: Task 17 fixture and Task 18 acceptance.

- [ ] **Step 1: Define the harness contract and first failing shared tests**

```python
class RuntimeAdapterHarness(Protocol):
    recovery_profile: Literal["durable_reference", "confined_process"]

    async def prepared_fixture(self) -> PreparedAdapterFixture: ...
    async def run_to_cut(self, cut: AdapterCut) -> CutResult: ...
    def durable_bytes(self) -> tuple[bytes, ...]: ...
    def provider_call_count(self, operation: str) -> int: ...


@pytest.mark.asyncio
async def assert_common_runtime_adapter_contract(harness: RuntimeAdapterHarness) -> None:
    fixture = await harness.prepared_fixture()
    assert fixture.provider_calls == 0
    assert fixture.initial_event_kinds[-3:] == (
        "task_attempt_started",
        "task_lease_acquired",
        "task_activity_prepared",
    )
    assert fixture.request_bytes == fixture.expected_request_bytes
```

- [ ] **Step 2: Confirm RED for both adapters**

Run: `uv run pytest tests/agent_runtime/test_opencode_conformance.py tests/agent_runtime/test_cursor_conformance.py -v`

Expected: harness adapters are not yet wired and shared assertions expose any contract difference.

- [ ] **Step 3: Implement common positive and negative assertions**

Cover strict request parsing; no instruction/routing mutation; exact request/config/policy digests; atomic prepare; no early dispatch; exact bind idempotency; durable cancel before call; quiescence/candidate/terminal ordering; receipt promotion once; reconcile before reclaim; same attempt/workspace; no ledger/store exposure; fixed host confinement; authorized secrets only; checkpoint/effect non-authority; and post-success provider-independent replay.

- [ ] **Step 4: Make capability differences explicit, not normalized away**

```python
if harness.recovery_profile == "durable_reference":
    assert (await harness.recover_live_activity()).status in {"running", "terminal"}
else:
    assert (await harness.recover_from_dead_host()).status == "indeterminate"
```

OpenCode must prove metadata/message rediscovery. Cursor must prove unknown process is indeterminate. The common suite must not demand fake durable recovery from Cursor or allow weaker ambiguous-create behavior from OpenCode.

- [ ] **Step 5: Run common, adapter, and engine suites**

Run: `uv run pytest tests/agent_runtime packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests packages/graph-engine/tests -q`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/agent_runtime
git commit -m "test(agent-runtime): enforce shared adapter conformance"
```

---

### Task 17: Add the neutral installed fixture and prove explicit rebinding/wheel isolation

**Files:**
- Create: `examples/agent-runtime-fixture/pyproject.toml`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/__init__.py`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/plugin.py`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/product.py`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/plugin-declaration.json`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/product-declaration.json`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/resources/instructions.txt`
- Create: `examples/agent-runtime-fixture/agent_runtime_fixture/resources/result-schema.json`
- Create: `examples/agent-runtime-fixture/manifests/opencode.json`
- Create: `examples/agent-runtime-fixture/manifests/cursor.json`
- Create: `tests/agent_runtime/test_fixture_rebinding.py`
- Create: `scripts/agent_runtime_wheel_smoke_test.sh`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: one installed neutral capability/product fixture, deterministic assembler, two explicit alias bindings, same canonical request bytes, same typed result, different adapter evidence, and four offline wheel-install cases.
- Consumes: Tasks 8, 12, 15, and 16.
- Consumer: Task 18 provider-live benchmark.

- [ ] **Step 1: Write failing rebinding integration tests**

```python
@pytest.mark.parametrize("target", ["runtime.opencode.execute", "runtime.cursor.execute"])
async def test_fixture_rebinds_without_engine_change(target: str) -> None:
    fixture = await resolve_fixture_composition(target)
    result = await run_fixture(fixture)
    assert fixture.captured_request_bytes == EXPECTED_AGENT_RUN_REQUEST_BYTES
    assert result.structured_result == {"status": "ok", "artifact": "result.json"}


async def test_adapter_rebinding_changes_lock_and_evidence_not_request() -> None:
    opencode, cursor = await run_both_fixture_bindings()
    assert opencode.request_bytes == cursor.request_bytes
    assert opencode.lock_digest != cursor.lock_digest
    assert opencode.result.evidence_digest != cursor.result.evidence_digest
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/agent_runtime/test_fixture_rebinding.py -v`

Expected: fixture wheel/manifests are absent.

- [ ] **Step 3: Implement the neutral declarative fixture**

The fixture owns one exact instruction resource, one strict result schema, and one deterministic assembler. Its `FrozenExecutionSelection` explicitly sets `provider_default`; no adapter adds a default. It registers no SUT executable and performs no ambient discovery.

```python
def assemble_request(resources: Mapping[str, bytes], config: JSONValue) -> AgentRunRequest:
    instruction = resources["fixture.instructions"].decode("utf-8")
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", instruction),),
        result_contract=_result_contract(resources["fixture.result-schema"]),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest=canonical_digest(config["permissions"]),
            limits={"max_seconds": 120},
        ),
        request_policy_digest=canonical_digest(config["request_policy"]),
        request_config_digest=canonical_digest(config),
    )
```

- [ ] **Step 4: Create two explicit compositions**

Both manifests select the same fixture product/plugin/workflow/input/resource bytes. Only alias target and adapter config/source differ. Assert both compile through `RegistryPlatform.resolve()` without engine changes.

- [ ] **Step 5: Implement four offline wheel isolation installs**

`scripts/agent_runtime_wheel_smoke_test.sh` builds committed HEAD wheels, installs them from local `--find-links` into clean temporary venvs, and verifies:

1. Graph Engine + contracts only;
2. Graph Engine + contracts + OpenCode + fixture;
3. Graph Engine + contracts + Cursor + fixture;
4. both adapters + fixture with one explicit selected binding.

Assert no install gains a default product/capability and importing each wheel does not import the other adapter or Assurance.

- [ ] **Step 6: Run fixture and smoke gates**

Run: `uv run pytest tests/agent_runtime/test_fixture_rebinding.py -q`

Run: `bash scripts/agent_runtime_wheel_smoke_test.sh`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock examples/agent-runtime-fixture tests/agent_runtime/test_fixture_rebinding.py scripts/agent_runtime_wheel_smoke_test.sh
git commit -m "test(agent-runtime): prove explicit adapter rebinding"
```

---

### Task 18: Enforce import/package boundaries and capture complete Phase 3 release evidence

**Files:**
- Modify: `.importlinter`
- Modify: `pyproject.toml`
- Modify: `scripts/packaging_smoke_test.sh`
- Create: `benchmark/agent-runtime-phase3/manifest.json`
- Create: `benchmark/agent-runtime-phase3/run-opencode.sh`
- Create: `benchmark/agent-runtime-phase3/run-cursor.sh`
- Create: `benchmark/agent-runtime-phase3/README.md`
- Create: `docs/superpowers/reports/2026-08-21-pure-graph-engine-phase3-acceptance.md`

**Interfaces:**
- Produces: mechanical package boundaries, complete CI/offline wheel proof, two provider-live single-item release runs, post-provider replay evidence, and final acceptance audit.
- Consumes: all prior tasks.
- Produces no runtime compatibility shim and performs no Assurance cutover.

- [ ] **Step 1: Add failing import contracts**

```ini
[importlinter:contract:graph-engine-provider-neutral]
name = graph engine must not import agent contracts or runtime adapters
type = forbidden
source_modules =
    graph_engine
forbidden_modules =
    agent_runtime_contracts
    agent_runtime_opencode
    agent_runtime_cursor

[importlinter:contract:adapter-independence]
name = runtime adapters are independent
type = independence
modules =
    agent_runtime_opencode
    agent_runtime_cursor

[importlinter:contract:runtime-adapters-no-old-runtime]
name = new adapters must not import Assurance runtimes
type = forbidden
source_modules =
    agent_runtime_opencode
    agent_runtime_cursor
forbidden_modules =
    assurance_agent
    assurance_kernel
```

Add contract-wheel independence from Graph Engine/adapters/Assurance and fixture-only downward dependencies.

- [ ] **Step 2: Confirm and close import/package boundaries**

Run: `uv run lint-imports`

Run exact source scans rejecting provider/business vocabulary in `packages/graph-engine/graph_engine` and legacy runtime imports in new wheels. Review every exception manually; do not add ignore edges for convenience.

- [ ] **Step 3: Add the committed provider-live manifest**

Pin fixture wheel/source digests, one-task graph and entrypoint, canonical request, initial workspace tree, declared write-set, result schema, expected file/content digest, adapter source/profile, external tool version, and success terminal criteria. The scripts consume only the manifest; they do not rewrite it or choose fallback values.

- [ ] **Step 4: Run one real OpenCode item to terminal**

Run: `bash benchmark/agent-runtime-phase3/run-opencode.sh`

Required evidence: exact metadata/session rediscovery, exact message admission, graph-ledger terminal success, expected committed output/digest, schema-valid result, adapter evidence digest, external OpenCode version/profile, no duplicate create/prompt, and successful local replay after provider state is made unavailable.

- [ ] **Step 5: Run one real Cursor item to terminal**

Run: `bash benchmark/agent-runtime-phase3/run-cursor.sh`

Required evidence: pinned executable/version, confinement preflight, exact cwd/argv/environment policy, strict `stream-json`, graph-ledger terminal success, expected committed output/digest, schema-valid result, adapter evidence digest, and successful local replay after the process/provider is unavailable.

- [ ] **Step 6: Run complete deterministic and packaging gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/agent_runtime -q
bash scripts/agent_runtime_wheel_smoke_test.sh
bash scripts/packaging_smoke_test.sh
uv run pytest -q
```

Expected: no new failure beyond an explicitly revalidated unrelated repository baseline; Phase 3 packages and tests are fully green. External live runs are mandatory acceptance evidence and are not replaced by fakes.

- [ ] **Step 7: Perform the hard-cut and credential audits**

Verify engine API, event schema, checkpoint schema, plugin conformance, lock golden, source digests, and fixture wheels are all v2/current. Assert no Phase 2 prototype resume fixture remains. Scan durable files/logs for all credential canaries. Confirm `aa` still uses the old runtime and no Assurance capability moved.

- [ ] **Step 8: Write the final acceptance report**

Record exact commands, pass counts, wheel hashes, provider versions, live evidence paths, replay results, source scans, import contracts, credential scans, and every Phase 3 acceptance criterion with a direct evidence link. Do not mark Phase 3 complete if either provider-live prerequisite is unavailable.

- [ ] **Step 9: Request independent two-axis review**

Use `superpowers:requesting-code-review` with the Phase 2 merge base and this spec. Require separate Standards and Spec verdicts, fix every Critical/Important finding with RED/GREEN evidence, rerun affected/full gates, and repeat until both axes report zero Critical and zero Important.

- [ ] **Step 10: Commit**

```bash
git add .importlinter pyproject.toml scripts/packaging_smoke_test.sh benchmark/agent-runtime-phase3 docs/superpowers/reports/2026-08-21-pure-graph-engine-phase3-acceptance.md
git commit -m "test(agent-runtime): complete phase three acceptance"
```

---

## Final Review and Handoff

- [ ] Read the complete diff from the Phase 2 merge base; do not review only the latest task.
- [ ] Verify the complete Phase 3 spec coverage matrix below has one or more concrete tests/evidence paths for every row.
- [ ] Run `rg -n 'TODO|TBD|FIXME|pass$|NotImplemented|placeholder'` over all new/modified Phase 3 files and classify every match.
- [ ] Run exact vocabulary scans proving Graph Engine has no adapter/provider/business/session vocabulary and new adapters have no old-runtime imports.
- [ ] Verify every public type annotation resolves and all frozen/canonical models reject unknown fields, mutable containers, NaN/Infinity, malformed digests, and secret values.
- [ ] Verify all engine/plugin/adapter/package version cuts are intentional and there are no compatibility aliases.
- [ ] Run the complete deterministic/CI/wheel gates from Task 18 on the final committed tree.
- [ ] Run both provider-live one-item scripts and verify replay after provider removal.
- [ ] Request final independent Standards and Spec reviews; Phase 3 is ready only with zero Critical and zero Important findings.
- [ ] Use `superpowers:finishing-a-development-branch` to present merge/PR/keep/discard options after all acceptance evidence is complete.

## Spec Coverage Matrix

| Spec area | Primary implementation task | Required evidence |
|---|---:|---|
| Generic values/events/errors and canonical encoding | 1 | activity model tests + v2 golden |
| Pure fold/state invariants and no duplicate transitions | 2 | legal/illegal/property fold matrix |
| Complete request projection and invocation metadata | 3 | scheduler capture tests |
| Fixed host transport, lock pin, confinement, secret port | 3, 7 | host protocol + lock + canary tests |
| Narrow activity port and CAS idempotency | 4 | race/ambiguous append tests |
| Create-before-prepare and workspace identity | 5 | ordering + lost/drift/symlink matrix |
| Reconcile-before-reclaim and same-attempt adoption | 6 | recovery decision/order matrix |
| Cancel, host receipt, quiescence, candidate, terminal | 7 | full receipt/cancel crash matrix |
| Provider-neutral request/result ownership | 8 | contracts strictness/schema/wheel isolation |
| OpenCode profile/preflight | 9 | closed config + profile fake |
| OpenCode generated session ID and ambiguous create | 10 | discovery/create fault matrix |
| OpenCode idempotent prompt, SSE, poll, cancel | 11 | admission/observation/cancel cuts |
| OpenCode terminal/result/evidence/credential boundary | 12 | reducer + retention + canary matrix |
| Cursor executable/process confinement | 13 | no-shell/cwd/env/confinement tests |
| Cursor receipt/parser/terminal/cancel | 14 | parser limits + process receipt matrix |
| Cursor unknown/crash/host identity semantics | 15 | crash/PID/host/cancel/canary matrix |
| Honest shared conformance | 16 | common black-box suite for both profiles |
| Explicit rebinding and isolated wheels | 17 | byte-identical request + four installs |
| Mechanical imports, full CI, live provider evidence | 18 | lint/static/tests/smokes/live reports |
| No Assurance migration or `aa` cutover | 18 | diff/source audit and acceptance report |

## Execution Choice

After this plan is approved, execute it in one of two ways:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`; one fresh implementer per task, then independent Spec and Standards review before advancing.
2. **Inline Execution:** use `superpowers:executing-plans`; implement sequentially in this task with the same RED/GREEN, regression, review, and commit boundaries.
