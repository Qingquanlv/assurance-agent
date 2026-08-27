# Task 18 Report — Publish Public Entrypoints and Audit the Full Graph

**Status:** DONE  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`  
**Branch:** `codex/pure-graph-engine-phase3-spec`  
**Does not amend:** `d9e4173`

## Summary

The canonical workflow now exposes exactly the 14 public entrypoints in spec §15.2. Task 17 leftovers are attached by reusing existing subgraphs: `execute` is generation through execution/quality/healing/report with no intake and no archive; standalone `archive` wraps `improvement-archive`; `issue-review` wraps `quality-issue-triage`; `issue-analyze` and `issue-reconcile` wrap `quality-issue-analysis`. Triplets are not duplicated.

`audit_full_graph(workflow, composition) -> GraphAuditResult` fail-closes on orphans, non-`end` dead ends, `runtime.*` / Phase 4 agent prepare IDs / test-only aliases, missing registry bindings, and uninventoried nodes. Phase 4 operation IDs already on the graph from Task 17 stay allowed. Agent nodes stay on `assurance.product.agent.<K>.*`. Both adapter compositions audit clean. Family-table validation already covered the new empty-family names; those entrypoints keep empty `selected_test_families`.

Inventory, both `product-declaration-*.json` files, and post-auth (unchanged allowlist) match the compiled slice. No `aa` cutover. No live adapters. No `assurance_agent` / `assurance_kernel`. No Assurance semantics in `graph-engine`. No pytest fixture named `request`.

## TDD evidence

### RED (Step 2)

```bash
uv run pytest tests/phase5/test_product_entrypoints.py tests/phase5/test_full_graph_audit.py tests/phase5/test_graph_binding_audit.py -q
```

```
ImportError: cannot import name 'audit_full_graph' from 'assurance_product.product'
AssertionError: Extra items in the right set: 'execute' 'archive' 'issue-review' 'issue-analyze' 'issue-reconcile'
12 failed, 23 passed
```

Failure reason: the five public entrypoints and `audit_full_graph` were absent (feature missing), not a typo.

### GREEN (Step 4)

```bash
uv run pytest tests/phase5/test_product_entrypoints.py tests/phase5/test_full_graph_audit.py tests/phase5/test_graph_binding_audit.py tests/phase5/test_graph_intake_and_triplets.py tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py tests/phase5/test_execution_quality_flow.py tests/phase5/test_issue_healing_flow.py tests/phase5/test_coverage_loop.py tests/phase5/test_report_flow.py tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py -q
```

```
92 passed, 1 warning in 645.00s
```

Focused Task 18 files alone: `43 passed, 1 warning`. The warning is the pre-existing kernel `CompiledWorkflow.schema` shadow. Focused `ruff check`, `ruff format --check`, and `pyright` on touched Python files: clean.

Covered: exact 14 entrypoints, typed root-input on every start closure, execute/archive/issue reachability, empty-family table for the new names, both-adapter orphan/dead-end/forbidden/binding/inventory audit, and the existing family/join/execution/issue/coverage/report/archive/STOP suite.

## Files changed

### Created

- `tests/phase5/test_product_entrypoints.py`
- `tests/phase5/test_full_graph_audit.py`
- `tests/phase5/test_graph_binding_audit.py`
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-18-report.md`

### Modified

- `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml` — `execute`, standalone `archive`, and three issue entrypoints
- `packages/assurance-product/assurance_product/product.py` — `GraphAuditResult` and `audit_full_graph`
- `packages/assurance-product/assurance_product/product-declaration-opencode.json`
- `packages/assurance-product/assurance_product/product-declaration-cursor.json`
- `tests/phase5/composition_harness.py` — `compiled_product_workflow` / `compiled_for`
- `tests/phase5/graph_inventory.py` — inventory dump helpers
- `tests/phase5/test_graph_intake_and_triplets.py` — all 14 public names and new graphs
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

## Self-review

- New graphs only reference existing triplets/subgraphs. YAML kinds stay `task` / `gate` / `join` / `subgraph` / `interrupt` / `end`.
- `execute` starts at `generation` and ends at `quality-report`. It does not reach intake/case or archive/retro/improvement.
- Agent capabilities remain the frozen 99 aliases. Graph aliases stay 99 + the six Task 17 Phase 4 operations (105).
- Direct `runtime.*`, Phase 4 agent prepare/finalize IDs, `assurance.{intake,generation,execution,quality,healing}.*`, and test-only aliases fail the audit.
- Family-table validation was already in `ProductInputV1`; new empty-family entrypoints use empty selection in `product_runner`.
- Did not cut over `aa`, add live adapters, or change `graph-engine`.

## Residuals

1. **`issue-reconcile` reuses `quality-issue-analysis`, same as `issue-analyze`.** Phase 4 `assurance.quality.reconcile-issues` stays off the graph because quality operations remain forbidden except via product agent aliases. A later task can attach the deterministic reconciler if the allowlist is opened.
2. **`execute` has no archive tail.** Archive is the standalone entrypoint; `full` still gates archive with `auto_archive`.
3. **Inventory lookup uses the worktree CWD** because wheel-extracted `product.py` is not next to `.superpowers/`. Missing inventory still fail-closes as all nodes uninventoried.
4. **Task 17 residuals remain:** interrupt `reject` continues like `approve`; extra-key resume is harness-only; collect/reconcile stay unmapped in `logical_steps`; live Phase 4 inputs are still `{change_id}` placeholders.
5. **`product_runner` stays a scripted in-process host.** No live OpenCode/Cursor.
