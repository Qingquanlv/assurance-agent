# Task 12 Report — Audit Pending V4/V5 Assurance Paths and Block Unbound Commit Safety

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 12  
**Worktree tip at start:** `c1964de` (Task 11 approved; Task 12 started)  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## What Was Implemented

### `assurance_agent/workflow/graph/resume_compatibility.py` (new)
- `TopologyCompatibilityReceiptV1` — StrictWireModel binding root, pinned digests, discovered-role digest, staged v6 topology object/digest, audit result, reachable-set digest, source sequence.
- Append-only event helper `receipt_to_event` / `event_to_receipt` for `topology_safety_compatibility_recorded`.
- `ResumeCompatibilityDecision` with stable reasons:
  - `legacy_commit_safety_semantics_unbound`
  - `legacy_topology_audit_unsafe`
  - `legacy_topology_profile_unreconstructable`
  - `topology_compatibility_receipt_corrupt`
- Audit trigger uses Task 9 discovered reachable assurance-codegen roles (and private-test contract writes), never frozen v4/v5 display status.
- Safe topology appends/reuses one bound receipt; bypass / unreconstructable v4 profile cannot.
- Topology receipt authorizes report/terminal-only remaining work only; pending codegen/fixer/effect/validator recovery returns `legacy_commit_safety_semantics_unbound`.
- Non-assurance v4/v5 roots with no assurance-codegen remaining work continue without a receipt.

### Event / fold / projection
- New `TopologySafetyCompatibilityRecordedEvent` in `graph_events.py` (exact duplicate fold idempotent; identity/payload drift = ledger corruption).
- `GraphProjection.topology_compatibility_receipt_id` folded in `checkpoint.py`.
- `replay_binding` validates receipts on v4/v5 bind (foreign root / digest drift → `topology_compatibility_receipt_corrupt`).

### Runtime barrier
- `_reach_recovery_barrier` evaluates compatibility **before** definition-dependent recovery/dispatch.
- Stages current v6 topology-semantics bytes for the audit receipt without rewriting the legacy root binding.
- Raises typed `ResumeCompatibilityBarrier(GraphRuntimeError)` carrying `decision` (no exception-text parsing).

### Driver status
- `DriverState.resume_compatibility_reason`
- `resume_compatibility_reason_from_error` / `project_resume_compatibility` surface the typed decision.

## Verify (Step 6)

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_resume_compatibility.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/integration/test_graph_runtime_faults.py
→ 104 passed

uv run ruff check assurance_agent/workflow/graph/resume_compatibility.py \
  assurance_agent/workflow/graph/runtime.py \
  assurance_agent/workflow/driver/driver_state.py \
  tests/unit/workflow/graph/test_resume_compatibility.py
→ All checks passed

uv run pyright → 0 errors
uv run lint-imports → 6 kept, 0 broken
```

## Files ready to stage (integrator owns commit)

**Created**
- `assurance_agent/workflow/graph/resume_compatibility.py`
- `tests/unit/workflow/graph/test_resume_compatibility.py`
- `.superpowers/sdd/task-12-report.md` (this report; replaces stale Trace-V2 Task 12 report)

**Modified**
- `assurance_agent/workflow/core/graph_events.py`
- `assurance_agent/workflow/graph/models.py`
- `assurance_agent/workflow/graph/checkpoint.py`
- `assurance_agent/workflow/graph/replay_binding.py`
- `assurance_agent/workflow/graph/runtime.py`
- `assurance_agent/workflow/driver/driver_state.py`
- `tests/unit/workflow/graph/test_replay_binding.py`
- `tests/integration/test_graph_runtime_faults.py`

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`
- Unrelated Task 14 WIP (`assurance_personas.py`, `opencode_adapter.py`, `test_opencode_adapter.py`) if present

Suggested commit message: `feat(graph): audit legacy assurance resume safety`

## Notes

- A topology receipt is necessary for audited pending assurance paths but never sufficient for commit-safety-bearing work.
- No old root event rewrite/backfill; v6 roots skip the legacy audit path.
- Operator exit remains Task 13 (`supersede`); this task only produces the typed block reason.
