# Task 7 Report — Inline Durable Effects, Acknowledgements, Retry Sidecar

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 7  
**Worktree tip at start:** `d5869f6`  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## What Was Implemented

### New modules
- `assurance_agent/workflow/graph/durable_effects.py` — `DurableEffectIntentV1`, `DurableEffectAcknowledgementV1`, `DurableEffectContext`, `EffectRegistry` / `EffectRegistration`, deterministic `derive_effect_id`, contract cardinality validation, `reconcile_effect`, unacked scan, integrity terminal helper. Production registry is empty.
- `assurance_agent/workflow/graph/effect_retry.py` — exact D14 `EffectRetryStateV1`, CAS `EffectRetryStore` (`load` / `schedule_next` / `clear_if_acknowledged`), explicit RFC 3339 `Z` parse/format, capped backoff, `RootTerminalFenceStateV1` + `RootEffectFenceStore.guard/prepare_terminal/commit_terminal/abort_prepared`.

### Wired into runtime
- `ExecutionContract.durable_effects` defaults `()`; catalog load rejects unregistered kinds against the production registry.
- `TaskResult.durable_effects` defaults empty; scheduler validates intents **before** one `TaskAttemptSucceededEvent` and embeds full canonical intents inline (no pending record).
- New graph events: `durable_effect_acknowledged`, `durable_effect_integrity_failed`; success event gains `durable_effects`.
- Checkpoint fold stores intents, requires commit before ack, idempotent exact duplicate acks, conflicting ack → integrity failure.
- Planner predecessor readiness: succeeded **and** `outputs_committed` **and** all effect IDs acknowledged.
- Recovery barrier: pending writes/publications first, then reconcile unacked effects (fence guard, retry sidecar on retryable, integrity terminal on permanent), then materialize/plan.
- Status exposes `unacknowledged_durable_effects`.

## Dark-ship preserved
- Production registry length/kinds empty (asserted).
- Packaged contracts select no `durable_effects`.
- Existing handlers return no intents; no packaged operation selects an effect.
- Root fence/retry seam unused by packaged supersede until Task 13; healing kinds register in Task 8.

## Verify (Step 9)

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_durable_effects.py \
  tests/unit/workflow/graph/test_effect_retry.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/integration/test_graph_runtime_faults.py
→ 223 passed

uv run ruff check assurance_agent/workflow/graph \
  assurance_agent/workflow/core/graph_events.py \
  tests/unit/workflow/graph
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations
```

## Files ready to stage (integrator owns commit)

**Create**
- `assurance_agent/workflow/graph/durable_effects.py`
- `assurance_agent/workflow/graph/effect_retry.py`
- `tests/unit/workflow/graph/test_durable_effects.py`
- `tests/unit/workflow/graph/test_effect_retry.py`

**Modify**
- `assurance_agent/workflow/graph/contracts.py`
- `assurance_agent/workflow/graph/models.py`
- `assurance_agent/workflow/core/graph_events.py`
- `assurance_agent/workflow/graph/checkpoint.py`
- `assurance_agent/workflow/graph/scheduler.py`
- `assurance_agent/workflow/graph/planner.py`
- `assurance_agent/workflow/graph/runtime.py`
- `assurance_agent/workflow/graph/status.py`
- `tests/unit/workflow/graph/test_contracts.py`
- `tests/unit/workflow/graph/test_planner.py` — succeeded fixture helper defaults `outputs_committed=True` for D14 readiness

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message (Step 10, integrator): `feat(graph): reconcile inline durable effects`

## Self-review
- Intent validation fails as `invalid_output` before success append.
- Ack requires matching inline intent + committed superstep.
- Retry sidecar never creates task attempts; inert after ack; CAS one winner; fence guard before schedule.
- Step 10 commit intentionally skipped per controller instructions.
