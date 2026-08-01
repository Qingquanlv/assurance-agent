# Task 9 Report — Separate Historical Role Discovery from Current Conformance

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 9  
**Worktree tip at start:** `2ca8c74`  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## What landed

- `historical_roles.py`: one semantic discovery manifest (`DiscoveredHistoricalLayerRoles` / `DiscoveredHistoricalAssuranceRoles`), structured discovery issues, canonical digest; selection-only structural lineage for zero-marker layers
- `historical_topology_v6.py`: v6 CFG/dominance/one-sided safety classifier (`wired | legacy_unwired | partial`); not a current compile gate
- Frozen v4/v5 display classifiers moved behind `classify_pinned_layer_topology_v4/v5` without improvement; goldens assert `("legacy_v4_unbound", false)` / `("legacy_v5_unbound", false)`
- `PinnedLayerTopology.semantics_id` / `semantics_bound`
- `HistoricalCompileContext.historical_roles` required; `ResolvedPinnedDefinition` / `FrozenDefinitionBinding` carry the same manifest
- `load_pinned_execution_definition` discovers once; selection / historical surface / bind / classify consume it (no name rediscovery)
- `specialty_replay` removed second `evaluate_layer_selection` call

## Verify Step 7

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_historical_roles.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py \
  tests/unit/eval/test_specialty_replay.py
→ 217 passed

uv run ruff check assurance_agent/workflow/graph/historical_roles.py \
  assurance_agent/workflow/graph/historical_topology_v6.py \
  assurance_agent/workflow/graph/definition_pinning.py \
  assurance_agent/workflow/graph/replay_schema.py \
  assurance_agent/workflow/graph/replay_binding.py \
  assurance_agent/eval/specialty_replay.py
→ All checks passed

uv run pyright → 0 errors
```

## Files ready to stage (integrator owns commit)

**Created**
- `assurance_agent/workflow/graph/historical_roles.py`
- `assurance_agent/workflow/graph/historical_topology_v6.py`
- `tests/unit/workflow/graph/test_historical_roles.py`
- `tests/unit/eval/test_specialty_replay.py`

**Modified**
- `assurance_agent/workflow/graph/{definition_pinning,replay_schema,replay_binding,compiler}.py`
- `assurance_agent/eval/specialty_replay.py`
- `tests/unit/workflow/graph/test_{compiler,four_layer_replay,packaged_schema_compiles,replay_binding}.py`

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `refactor(graph): separate historical assurance semantics`

## Notes

- Current `classify_pinned_layer_topology` remains the frozen v5 unbound display alias
- Version dispatch in replay binding: v4→v4 classifier, v5→v5, v6+→v6 with discovered roles
- Topology semantics manifest / event schema v6 root binding deferred to Tasks 10–11
