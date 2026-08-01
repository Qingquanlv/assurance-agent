# Task 4 Report — Declared-Only Sidecars + Attempt Input Snapshots

## Status: DONE

Branch tip remained `775a513` (no commit). Edit + test only.

## What changed

### New
- `assurance_agent/workflow/graph/task_inputs.py` — D13 wire models (`TaskInputSnapshotEntryV1`, `TaskInputSnapshotV1`, `PlanFixerRuntimeContextV1`), `capture_task_input_snapshot` / `load_task_input_snapshot`, plan-fixer Runtime Context render/bind/assert helpers, dormant prepare/resume binding helpers.
- `tests/unit/workflow/graph/test_task_input_snapshot.py` — strict model tests, sidecar visibility + aliased-root snapshot, begin-attempt ordering/crash-retry, lock deferral, prompt binding, closed AST consumer inventory.

### Modified
- `assurance_agent/workflow/graph/workspace.py` — `sidecar_root` on `WorkspaceBackend.create`, explicit `tree_manifest_path` / `sidecar_root` on `TaskWorkspace.from_materialized_root`, materialize writes control manifest to sidecar; declared-only skips convenience git.
- `assurance_agent/workflow/core/graph_events.py` — optional `input_snapshot_id` / `runtime_context_sha256` on start/success; new `TaskSchedulingDeferredEvent`.
- `assurance_agent/workflow/graph/models.py` — projection fields for snapshot/context/deferral ordinal.
- `assurance_agent/workflow/graph/checkpoint.py` — fold start/success snapshot identity match; fold deferrals (highest ordinal, idempotent ID, payload conflict = corruption).
- `assurance_agent/workflow/graph/leases.py` — pending tasks honor `next_retry_at` from scheduling deferral without consuming attempt numbers.
- `assurance_agent/workflow/graph/scheduler.py` — reorder `_begin_attempt` (reserve → materialize → snapshot CAS → started → dispatch); lock conflict → `task_scheduling_deferred` (no attempt/budget/failed); success repeats snapshot/context IDs; crash-cut cleanup for pre-started unreachable sidecar/CAS.
- `assurance_agent/workflow/graph/agent_api.py` — `AgentRequest.runtime_context_sha256`; prompt Runtime Context bind/reject mismatch (dormant unless context supplied).
- `assurance_agent/workflow/graph/handlers/agent.py` — skip host-link policy for `declared_only`.
- Tests: `test_workspace.py`, `test_scheduler.py` (lock-conflict shape), `test_trace_recovery_workflows.py` (sidecar threading).

### Left alone (per instructions)
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

### Plan file listed but unchanged
- `assurance_agent/workflow/graph/contracts.py` — `read_isolation: declared_only` already present; no Task 4 API change required.

## Dark-ship status

- No packaged contract flipped to `declared_only`.
- Non-declared / legacy workspaces still compile and run; host links and convenience git remain for non-declared agents.
- Historical events without new fields still fold.
- Plan-fixer Runtime Context injection path exists but is not selected by current contracts (Task 15).
- Snapshot capture/start binding activates only when a contract uses `read_isolation: declared_only`.

## Verify commands / results

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_task_input_snapshot.py \
  tests/unit/workflow/graph/test_workspace.py \
  tests/unit/workflow/graph/test_read_isolation.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/integration/test_trace_recovery_workflows.py
# → 228 passed

uv run ruff check assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/graph tests/unit/workflow/graph
# → All checks passed

uv run pyright
# → 0 errors, 0 warnings, 0 informations
```

## Not committed

Integrator owns staging/commit. Suggested message from plan:

`feat(graph): bind declared task input snapshots`
