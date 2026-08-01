### Task 13: Add Audited Legacy-Root Supersede and Single-Use V6 Replacement

**Files:**
- Create: `assurance_agent/workflow/graph/supersede.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/status.py`
- Modify: `assurance_agent/workflow/driver/driver_state.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Create: `tests/unit/workflow/graph/test_supersede.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`

**Interfaces:**
- Produces: `SupersedeAction`, `SupersedeEligibility`, `GraphInvocationSupersededEvent`, subtree digest/fence, `GraphRuntime.supersede(...)`, replacement authorization consumption, and `aa workflow supersede`.
- Consumes: latest active root, typed legacy-block decision, descendant invocation closure, lease/open-attempt/superstep/write/publication/effect/retry state, Task 7's already-frozen `RootEffectFenceStore`, current v6 staged request, resolved canonical params, operator identity, and reason.
- Preserves: normal active-root and `restart: once` guards. Only the exact unused replacement authorization bypasses them.

- [ ] **Step 1: Add CLI shape and eligibility tests**

  Accept exactly `--change`, `--invocation`, `--action rerun-v6|stop`, non-empty `--who`, non-empty `--reason`, and optional JSON `--params` only for rerun. Reject child ID, other change/entrypoint, non-latest or unrelated terminal root, non-blocked root, stop with params, invalid params, or a staged request that is not v6.
- [ ] **Step 2: Add complete subtree quiescence and retry-fence tests**

  Reject any live lease, open/running attempt, prepared/uncommitted superstep or write set, pending synchronized publication, unacknowledged effect, active retry sidecar, or concurrent child creation anywhere in the canonical descendant closure. Consume the frozen root guard/prepare/commit protocol from Task 7; `GraphRuntime.supersede(...)`, effect reconciliation, and `EffectRetryStore.schedule_next(...)` all follow the already-tested guard-before-progression order. Race a due retry against supersede and prove neither can create sidecar state after preparation/commit or deadlock. Assert a real v6 root pinned before this task retains byte-identical `runtime_commit_safety/v1` bytes/digest and passes compatibility across the new caller.
- [ ] **Step 3: Add terminal-fence tests**

  After supersede, root and all descendants project stopped/superseded for scheduling; direct child resume returns the stable superseded result; recovery does not adopt a descendant; late worker success/publication/effect acknowledgement is rejected by the fence. Existing status renderers may say `stopped`, but the typed audit event remains queryable.
- [ ] **Step 4: Add replacement/crash/concurrency tests**

  Cover before append, after append before new root start, after root start before return, two concurrent exact commands, conflicting params/reason, generic run without authorization, import-checkpoint, another entrypoint, and second consumption. Exact retry must start or resume at most one replacement root bound to the superseded root, authorization, params digest, and staged definition-request digest.
- [ ] **Step 5: Run tests and observe the permanent-active-root behavior**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  ```

  Expected: command/API are absent and the ordinary active guard prevents a fresh v6 root.
- [ ] **Step 6: Implement two durable transactions for fence and authorization consumption**

  Stage and validate the exact current v6 request before terminalizing. First acquire `RootEffectFenceStore.guard`, write a prepared terminal-fence CAS, then acquire the progression lock, rescan eligibility, and durably append one deterministic supersede event/authorization; commit the terminal fence before releasing the root guard. A crash/retry resolves a prepared state from the authoritative event: commit it when the event exists, otherwise the same command may safely abort it. Second, under a new progression transaction, atomically consume that authorization and append or recover the one replacement root-start pair. A crash between transactions is intentional and retryable; do not re-enter the progression lock. Load the already-staged request by digest on retry.
- [ ] **Step 7: Wire the CLI to typed results**

  Never infer eligibility from error text. Return stable nonzero exits for ineligible/conflicting requests and the new/resumed invocation ID for rerun. `stop` produces no replacement authority.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  uv run ruff check assurance_agent/workflow/graph/supersede.py assurance_agent/workflow/graph/runtime.py assurance_agent/commands/workflow_cmd.py tests/unit/workflow/graph/test_supersede.py
  uv run pyright
  ```

  Expected: every named crash/concurrency cut converges to one fence and at most one replacement root.
- [ ] **Step 9: Commit the audited operator exit**

  ```bash
  git add assurance_agent/workflow/graph/supersede.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/status.py \
    assurance_agent/workflow/driver/driver_state.py \
    assurance_agent/commands/workflow_cmd.py \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  git commit -m "feat(workflow): add audited legacy root supersede"
  ```

