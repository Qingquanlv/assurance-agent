# Task 15 Report — Add Four Generation Families and the Exact Selected-Family Join

**Status:** DONE  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`  
**Branch:** `codex/pure-graph-engine-phase3-spec`  
**Does not amend:** `fec7e31`

## Summary

Kept `intake` and `case` ending after case-review. Added public `full` entrypoint that runs intake/case, then a four-family generation fan-out, then `generation/join-selected` (`join: all`), then `end`. No execution/quality/healing/report (Task 16).

API/E2E each have plan, plan-review, codegen, and codegen-fix triplets. Fuzz/Performance have their Phase 4 plan/review/codegen sequences. Agent capabilities are only `assurance.product.agent.<K>.{prepare,execute,finalize}` aliases. Unselected families take an explicit skip gate; `join: all` always waits on the four family terminals (`*-done`). Skip tokens are not counted as `completed_generation_families`. `join_expected` equals `selected_test_families`. Join projection is `root_pointer /selected_test_families` plus `all_predecessor_tokens` (sorted), so `join_output` is order-independent.

Guards read only the validated `ProductInputV1.selected_test_families` tuple. Empty families still fail `validate_for_entrypoint("full")`. Unknown families still fail `ProductInputV1` validation. `product_runner` is a deterministic in-process Engine run with a success `TaskExecutionHost`; no live OpenCode/Cursor and no `aa`.

Graph inventory and post-auth now require this slice’s 54 aliases, still ⊆ the frozen 99. `product-declaration-*.json` regenerated so `manifest().workflow` equals `load_canonical_workflow()`.

No `assurance_agent` / `assurance_kernel` imports. No Assurance semantics added to `graph-engine`. No pytest fixture named `request`.

## TDD evidence

### RED (Step 2)

```bash
uv run pytest tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py -q
```

```
20 failed, 1 warning
AssertionError: workflow stops after the intake/case slice
```

Failure reason: `full` and generation/join were absent (feature missing), not a typo.

### GREEN (Step 4)

```bash
uv run pytest tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py tests/phase5/test_graph_intake_and_triplets.py tests/phase5/test_product_composition.py tests/phase5/test_binding_coverage.py tests/phase5/test_composition_authority.py -q
```

```
43 passed, 1 warning in 204.58s
```

The warning is the pre-existing kernel `CompiledWorkflow.schema` shadow. Focused `ruff check`, `ruff format --check`, and `pyright` on touched Python files: clean.

Covered: 15 non-empty family subsets, single-family isolation, forward/reverse join projection equality, intake/case unchanged, both providers, inventory lock, exact-99 composition/auth.

## Files changed

### Created

- `tests/phase5/test_generation_branches.py`
- `tests/phase5/test_selected_family_join.py`
- `tests/phase5/product_runner.py` — session `product_runner` factory; success host; join trace
- `tests/phase5/runtime_composition.py` — in-process FrozenComposition for the canonical YAML without live adapter bindings

### Modified

- `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml` — `full` entrypoint, generation fan-out/skip/join, 14 family triplet subgraphs
- `packages/assurance-product/assurance_product/product-declaration-opencode.json`
- `packages/assurance-product/assurance_product/product-declaration-cursor.json`
- `tests/phase5/graph_inventory.py` — `GENERATION_FAMILIES`, `generation_prepare_ids()`
- `tests/phase5/test_graph_intake_and_triplets.py` — generation graphs/aliases; `full` entrypoint
- `tests/phase5/conftest.py` — load `product_runner` plugin
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

## Self-review

- YAML compiles under current engine kinds (`task`, `gate`, `join`, `subgraph`, `end`). `join: all` has four distinct family-terminal predecessors.
- Select gates use `'<family>' in selected_test_families` over a `root_pointer` object projection. No invented expression operators.
- Agent capabilities are only Task 1 aliases. Direct `runtime.*.execute` and Phase 4 `assurance.generation.*.prepare` IDs remain forbidden by inventory/auth.
- `intake`/`case` still end after case-review. `full` is the only new public entrypoint.
- Skip tokens are family-terminal gate outputs; completed families are counted only from generation task activations.
- Registry/lock compares remain exact-99. Graph bindings are the 54-alias intake+generation subset.
- Did not add execution/quality/healing/report (Task 16) or remaining public entrypoints (Task 18).

## Residuals

1. **`execute` entrypoint is still an empty inventory slot.** Task 16/18 can attach execution after `join-selected` or reuse `full`’s generation slice.
2. **Codegen-fix is linear on the selected API/E2E path.** Spec §15.5’s bounded needs-fix route is present as a step, not as a review-output loop. A later task can gate it on typed review/codegen output without changing the triplet set.
3. **`product_runner` uses a success-host test composition**, not the live OpenCode execute binding, so activity-recovery and secret handles are not exercised here.
4. **`phase3-live-closeout.md` was already dirty in the worktree and was left unstaged.**
