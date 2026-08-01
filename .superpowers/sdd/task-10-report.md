# Task 10 Report — Build the Three Closed Semantics Manifests

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 10  
**Worktree tip at start:** `caaa253` (Task 9 approved; Task 10 started)  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## What Was Implemented

Canonical recoverable descriptor bytes / object digest / semantic digest APIs for:

| Semantic ID | Module | Bytes / digests |
|---|---|---|
| `plan_gate_semantics/v1` | `orchestration/gate_semantics.py` | `gate_semantics_bytes()`, `gate_semantics_object_digest()`, existing `gate_semantics_digest()` (semantic) |
| `historical_topology_safety/v1` | `graph/topology_semantics.py` | `topology_safety_semantics_bytes()`, `*_object_digest()`, `*_digest()` |
| `runtime_commit_safety/v1` | `graph/runtime_commit_safety.py` | `commit_safety_semantics_bytes()`, `*_object_digest()`, `*_digest()` |

### Design notes

- Gate manifest is no longer digest-only: recoverable canonical JSON bytes include `semantics_id`, schema/runtime versions, dependency inventory, consumers, and `semantic_digest`.
- `gate_semantics_digest()` aggregate algorithm preserved (same `symbols` payload shape) so v4/v5 live bindings stay compatible; `object_digest` is `sha256(canonical_bytes)`.
- Inventory constants live beside registries (`precommit`, `durable_effects`, `effect_retry`, `healing/effects`); `runtime_commit_safety` concatenates them and fail-closes on unregistered / listed-but-unconsumed consumers.
- Named fence/retry methods are digested individually (`RootEffectFenceStore.guard/prepare/commit/...`); unrelated module attributes do not change `runtime_commit_safety/v1`.
- Topology inventory covers discovery, CFG, dominance, truth-table, wiring statuses, and runtime versions; `WIRING_STATUSES` added next to `V6_SEMANTICS_ID`.
- AST digests use `textwrap.dedent` so class methods parse cleanly without hashing whole mutable modules.

## Verify (Step 6)

```text
uv run pytest -q \
  tests/unit/workflow/orchestration/test_gate_semantics_manifest.py \
  tests/unit/workflow/graph/test_topology_semantics.py \
  tests/unit/workflow/graph/test_runtime_commit_safety.py
→ 160 passed

uv run ruff check assurance_agent/workflow/orchestration/gate_semantics.py \
  assurance_agent/workflow/graph/topology_semantics.py \
  assurance_agent/workflow/graph/runtime_commit_safety.py \
  tests/unit/workflow/graph
→ All checks passed

uv run pyright → 0 errors
```

## Files ready to stage (integrator owns commit)

**Created**
- `assurance_agent/workflow/graph/topology_semantics.py`
- `assurance_agent/workflow/graph/runtime_commit_safety.py`
- `tests/unit/workflow/graph/test_topology_semantics.py`
- `tests/unit/workflow/graph/test_runtime_commit_safety.py`

**Modified**
- `assurance_agent/workflow/orchestration/gate_semantics.py`
- `assurance_agent/workflow/graph/precommit.py`
- `assurance_agent/workflow/graph/durable_effects.py`
- `assurance_agent/workflow/graph/effect_retry.py`
- `assurance_agent/workflow/healing/effects.py`
- `assurance_agent/workflow/graph/historical_topology_v6.py`
- `tests/unit/workflow/orchestration/test_gate_semantics_manifest.py`
- `.superpowers/sdd/task-10-report.md` (this report)

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `feat(graph): define closed runtime semantics manifests`

## Deferred (per plan)

- Root/child definition fields and event schema v6 → Task 11
- Field-consumer guards for object ID/digest through runtime/export → Tasks 11/17
