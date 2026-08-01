# Task 7 Report — Inline Durable Effects, Acknowledgements, Retry Sidecar

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 7  
**Worktree tip at fix pass start:** `e453b51` (prior land)  
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

## Follow-up fixes (this pass)

| ID | Fix |
|----|-----|
| P1 | `_reconcile_durable_effects` catches `RootTerminalFenceError` from `schedule_next` (same clean no-progress suppress as reconcile path) |
| P2 | Fold of `durable_effect_acknowledged` requires kind / reconciler_semantics_digest / payload_sha256 match the exact inline intent |
| P2 | Fan-out `_seed_fan_out` / `_decide_fan_out` / `fan_out_state_updates` use `_task_ready_as_predecessor`; `_has_inflight` treats uncommitted/unacked success as in-flight |
| P2 | `TaskAttemptStartedEvent.target` + `TaskProjection.target` stamped from `ExecutableTask.target`; recovery builds `DurableEffectContext.target` from that contract/operation identity |

### Regression coverage
- `test_reconcile_uses_operation_target_and_suppresses_fence_on_retry_schedule`
- `test_crash_cut_before_success_leaves_no_inline_intent`
- `test_durable_effect_ack_must_bind_inline_intent_digests`
- `test_fan_out_waits_for_committed_and_acked_children`
- Fan-out / planner fixture helpers default `outputs_committed=True` for succeeded tasks

## Dark-ship preserved
- Production registry length/kinds empty (asserted).
- Packaged contracts select no `durable_effects`.
- Existing handlers return no intents; no packaged operation selects an effect.
- Root fence/retry seam unused by packaged supersede until Task 13; healing kinds register in Task 8.

## Verify

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_durable_effects.py \
  tests/unit/workflow/graph/test_effect_retry.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/integration/test_graph_runtime_faults.py
→ 226 passed

uv run ruff check assurance_agent/workflow/graph \
  assurance_agent/workflow/core/graph_events.py \
  tests/unit/workflow/graph
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations
```

Also green locally: `test_fanout_budget.py` + `test_planner.py` (311 with the above extras).

## Files ready to stage (integrator owns commit)

**Uncommitted fix delta (on top of `e453b51`)**
- `assurance_agent/workflow/core/graph_events.py` — `TaskAttemptStartedEvent.target`
- `assurance_agent/workflow/graph/models.py` — `TaskProjection.target`
- `assurance_agent/workflow/graph/scheduler.py` — stamp target on started
- `assurance_agent/workflow/graph/checkpoint.py` — fold target + ack digest binding
- `assurance_agent/workflow/graph/runtime.py` — fence catch on schedule_next; context.target from projection
- `assurance_agent/workflow/graph/planner.py` — fan-out / inflight / reduce readiness
- `tests/unit/workflow/graph/test_durable_effects.py`
- `tests/unit/workflow/graph/test_checkpoint.py`
- `tests/unit/workflow/graph/test_fanout_budget.py`

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested follow-up commit message: `fix(graph): harden durable-effect ack, fence, and fan-out readiness`

## Self-review
- Intent validation fails as `invalid_output` before success append.
- Ack requires matching inline intent digests + committed superstep.
- Retry sidecar never creates task attempts; fence during retry schedule cannot escape the recovery barrier.
- Fan-out parent/aggregate/reduce cannot settle on bare succeeded children.
