# Task 11 Report — Pin All Three Semantics Objects and Flip New Roots to Event Schema V6

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 11  
**Worktree tip at start:** `9fb73c3` / HEAD `757207e` (Task 10 approved; Task 11 started)  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## What Was Implemented

### Six v6 fields (all-or-none)
On `GraphInvocationStartedEvent`, `GraphProjection`, `PinnedDefinitionRequest`, and `InvocationDefinitionBinding`:

| Pair | Object ID | Semantic digest |
|---|---|---|
| Gate | `gate_semantics_object_id` | `gate_semantics_digest` (existing) |
| Topology | `topology_safety_semantics_object_id` | `topology_safety_semantics_digest` |
| Commit safety | `commit_safety_semantics_object_id` | `commit_safety_semantics_digest` |

- Pure event-model layer enforces all-or-none for v6; v1–v5 fixtures remain valid without the new fields (and reject synthetic v6 identities).
- Object-byte/digest mismatch and unknown semantic IDs fail in `verify_pinned_definitions` / replay load — no `workflow.core → workflow.graph/storage` dependency.

### Staging / inheritance
- `stage_pinned_definitions` write-once stages gate/topology/commit-safety bytes under `.graph-runtime/{gate,topology,commit-safety}-semantics/{object_id}.json`.
- `bind_root_definitions(..., event_schema_version=6)` and `inherit_child_definitions` carry exact six fields + recoverable bytes.
- Child coverage: assurance / layer-cycle / retro / issue-review / improvement-review inherit; non-assurance inherits without classification.
- Phase-local consumer inventory `V6_SEMANTIC_FIELD_CONSUMERS` registers `evidence_export` obligation for Task 17.

### Version dispatch
- v4/v5 classifiers retain `legacy_v4_unbound`/`false` and `legacy_v5_unbound`/`false` goldens.
- v6 calls classifier only after digest-verified topology bytes (`historical_topology_safety/v1` / `semantics_bound=true`).
- Migration accepts schema version 6 only with complete bindings; v1–v5 goldens unchanged; separate `tests/fixtures/workflow/graph-events-v6.jsonl`.

### Writer flip (after tests)
- `GraphRuntime` root/import paths and `runtime_factory` default/current request: **5 → 6**.
- Fresh-root helpers (`helpers_graph_v3`, integration asserts) require v6 + six fields; explicit legacy helpers still build v5.

### Perf note
Process-local `lru_cache` on semantics digest/bytes helpers and `current_v6_semantic_identity()` so live compatibility checks do not re-AST-digest on every resolve.

## Verify (Step 9)

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_policy_digest_event.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/workflow/driver/test_runtime_factory_compilation.py \
  tests/unit/test_events.py \
  tests/integration/test_graph_interrupt_v3.py \
  tests/integration/test_graph_runtime.py \
  -k 'not test_prepared_publication_blocks_later_change_after_apply_before_ack_crash'
→ 275 passed, 1 deselected

uv run ruff check assurance_agent/workflow/core assurance_agent/workflow/graph \
  assurance_agent/workflow/driver/runtime_factory.py tests/unit/workflow
→ All checks passed

uv run pyright → 0 errors
uv run lint-imports → 6 kept, 0 broken
```

### Known pre-existing hang (not Task 11)
`test_prepared_publication_blocks_later_change_after_apply_before_ack_crash` busy-loops in `_reach_recovery_barrier` / `project()` (>2000 calls). Reproduced on clean HEAD without Task 11 edits; deselected for this gate.

## Files ready to stage (integrator owns commit)

**Created**
- `tests/fixtures/workflow/graph-events-v6.jsonl`
- `.superpowers/sdd/task-11-report.md` (this report; replaces stale trace-plan Task 11 report)

**Modified**
- `assurance_agent/workflow/core/graph_events.py`
- `assurance_agent/workflow/core/migrate_events.py`
- `assurance_agent/workflow/graph/models.py`
- `assurance_agent/workflow/graph/compiler.py`
- `assurance_agent/workflow/graph/definition_pinning.py`
- `assurance_agent/workflow/graph/checkpoint.py`
- `assurance_agent/workflow/graph/runtime.py`
- `assurance_agent/workflow/graph/replay_binding.py`
- `assurance_agent/workflow/graph/scheduler.py`
- `assurance_agent/workflow/graph/topology_semantics.py` (digest cache)
- `assurance_agent/workflow/graph/runtime_commit_safety.py` (digest cache)
- `assurance_agent/workflow/orchestration/gate_semantics.py` (digest cache)
- `assurance_agent/workflow/driver/runtime_factory.py`
- `tests/unit/workflow/graph/test_policy_digest_event.py`
- `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- `tests/unit/test_events.py`
- `tests/helpers_graph_v3.py`
- `tests/integration/test_graph_runtime.py`

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `feat(graph): bind runtime semantics in event schema v6`

## Deferred
- Task 12: legacy resume / `legacy_commit_safety_semantics_unbound`
- Task 17: close `evidence_export` consumer against this inventory
