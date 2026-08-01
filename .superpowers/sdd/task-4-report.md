# Task 4 Report — Declared-Only Sidecars + Attempt Input Snapshots

## Status: DONE

Branch tip at start of fix pass: `8541d36`. Edit + test only (no commit).

## What changed (this fix pass)

### P0 — Deferred lock conflicts reselectable when due
- `assurance_agent/workflow/graph/planner.py` — `pending` + `next_retry_at` (scheduling deferral) is added to `retry_tasks` instead of treated as permanently in-flight; plain `pending` without `next_retry_at` stays in-flight. Fan-out children get the same treatment. `_has_inflight` excludes backoff-gated deferrals.
- `assurance_agent/workflow/graph/runtime.py` — `_earliest_retry_at` includes `pending` with `next_retry_at` so `_drive` sleeps until due before re-planning.
- `assurance_agent/workflow/graph/scheduler.py` — on lock conflict, durable-ize any not-yet-written `node_activated` from `plan.strict_events` before emitting `task_scheduling_deferred` (prepared/synchronized waves otherwise skip activation when the lock is never held). Still no started+failed lock-conflict shape.

### P2 — Step 3 coverage + AST zero-ref
- `tests/unit/workflow/graph/test_task_input_snapshot.py`
  - Real `GraphRuntime` / `plan_superstep` / `_drive` path: clock past `next_retry_at`, lock released, exactly one `task_attempt_started` with `attempt_number == 1`.
  - `crash_after_started`: started event keeps a loadable snapshot; abandon + retry uses attempt 2.
  - Capture-time wrong-tree / wrong-review rejection.
  - Empty `matched_claims` schema reject.
  - Closed inventory: plan-fixer skills + declared-only agent surface modules must contain zero `events.jsonl` references (text + AST string check).
- `assurance_agent/_resources/skills/aa-api-plan-fixer/SKILL.md` and `aa-e2e-plan-fixer/SKILL.md` — mode/source-review authority via typed Runtime Context; removed `events.jsonl` reads.
- `assurance_agent/workflow/graph/task_inputs.py` — require non-empty `matched_claims`; capture-time `source_review_sha256` byte check against workspace review file.

### Left alone (per instructions)
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## How reselection works (D13)

1. Lock conflict → `task_scheduling_deferred` folds to `TaskProjection(status="pending", next_retry_at=..., attempts_used=0)` (and durable `node_activated` if the prepared-wave path had not written it yet). No attempt / budget / failed event.
2. `_drive` waits until `_earliest_retry_at` (pending or failed).
3. `plan_superstep` reselects the same `task_id` into `retry_tasks` when `pending` + `next_retry_at` is set.
4. `next_attempt_decision` returns `wait` before due, else `start` with `attempt_number = attempts_used + 1` → **1** on first real attempt after release.
5. Continued conflict after due emits the next deferral ordinal with capped backoff; still no started+failed.

## Dark-ship status

Unchanged from prior Task 4 landing: no packaged contract flipped to `declared_only`; Runtime Context injection remains dormant until Task 15; snapshot binding activates only for `read_isolation: declared_only`.

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
# → 233 passed

uv run ruff check assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/graph tests/unit/workflow/graph
# → All checks passed

uv run pyright
# → 0 errors (after typing fixes)
```

## Not committed

Integrator owns staging/commit. Do not include cursor-loop-helpers dirty files.
