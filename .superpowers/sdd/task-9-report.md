# Task 9 Report — Separate Historical Role Discovery from Current Conformance

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 9  
**Worktree tip at start of fix pass:** `139c69c`  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## Fix pass (P1 / P2)

| Priority | Item | Resolution |
|----------|------|------------|
| P1 | Finite-domain selection safety | `_selection_safety_diagnostics` builds the pinned finite assignment table, rejects `unbounded_param_domain`, and requires evaluability over that domain via `expressions_truth_equivalent`; unknown/forbidden builtins still fail first |
| P1 | `missing_unique_role` fail-closed at load | `load_pinned_execution_definition` blocking set now includes `missing_unique_role` → `pinned_historical_roles_invalid` |
| P2 | Unaudited remediation → partial | `_remediation_diagnostics` requires interrupt `fix_and_proceed` to regenerate via mechanical / reviewer / fixer; mismatch → `unaudited_remediation_return` partial |

## What was already landed (committed @ 139c69c)

- `historical_roles.py` / `historical_topology_v6.py`
- Frozen v4/v5 display classifiers + `semantics_id` / `semantics_bound` goldens
- Roles carriers on compile/pin/bind; specialty_replay no second selection rediscovery

## Verify Step 7 (post-fix)

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
→ 220 passed

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

**Modified in fix pass**
- `assurance_agent/workflow/graph/historical_topology_v6.py`
- `assurance_agent/workflow/graph/definition_pinning.py`
- `tests/unit/workflow/graph/test_historical_roles.py`

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `fix(graph): harden historical selection and remediation safety`

## Notes

- Stable diagnostics: `unbounded_param_domain`, `unevaluable_selection_over_finite_domain`, `unaudited_remediation_return`
- Zero-marker selection-only lineages still load; incomplete activated-cycle discovery with `missing_unique_role` does not
