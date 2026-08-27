# Task 17 Report — Add Archive, Retro, Improvement, STOP, and Interrupt Paths

**Status:** DONE  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`  
**Branch:** `codex/pure-graph-engine-phase3-spec`  
**Does not amend:** `c25b2d6`

## Summary

`full` still ends at mandatory report. When `ProductInputV1.auto_archive` is true, `improvement.archive` runs after report; when false, `full` ends at report. Retro and the five Improvement names are independent entrypoints and are not appended to `full`. Agent-backed archive/retro/review steps use only `assurance.product.agent.<K>.*` aliases. Phase 4 collect/reconcile/evaluate/export/apply/rollback operations keep their Phase 4 IDs. The compiled graph now uses all 99 aliases plus those six operations.

Typed STOP is `TaskOutcome.stopped(reason=...)`. Healing-disallowed finalize and infrastructure/coverage report STOP stay business STOP. Spec-enumerated `needs-human` is a `kind: interrupt` node with closed `approve`/`reject` actions. Resume uses the same invocation and exact lock; extra keys or unknown actions fail. Nested STOP is not translated into completion.

`product_runner` stays deterministic in-process. No live adapters. No `aa`. `intake`/`case` graphs are unchanged.

No `assurance_agent` / `assurance_kernel` imports. No Assurance semantics added to `graph-engine`. No pytest fixture named `request`.

## TDD evidence

### RED (Step 2)

```bash
uv run pytest tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py -q
```

```
9 failed, 1 warning
TypeError: product_runner.<locals>.factory() got an unexpected keyword argument 'entrypoint'
AttributeError: 'ProductRun' object has no attribute 'run_to_terminal'
```

Failure reason: archive/retro/improvement/interrupt runner API and routes were absent (feature missing), not a typo.

### GREEN (Step 4 + Task 15–16 report)

```bash
uv run pytest tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py tests/phase5/test_report_flow.py tests/phase5/test_graph_intake_and_triplets.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_activity_recovery.py -q
```

```
156 passed, 1 warning in 177.06s
```

Related Task 15–16 / composition follow-up:

```
41 passed, 1 warning
```

The warning is the pre-existing kernel `CompiledWorkflow.schema` shadow. Focused `ruff check`, `ruff format --check`, and `pyright` on touched Python files: clean.

Covered: archive-on-request vs skip, independent retro/improvement entrypoints, interrupt/resume, invalid resume input, healing-disallowed STOP, nested STOP, infrastructure STOP after report, and reported terminal success.

## Files changed

### Created

- `tests/phase5/test_archive_retro_improvement.py`
- `tests/phase5/test_stop_and_interrupts.py`
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-17-report.md`

### Modified

- `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml` — optional archive tail; retro + five Improvement entrypoints; `needs-human` interrupts
- `packages/assurance-product/assurance_product/product.py` — post-auth allows Phase 4 operation IDs; forbids `runtime.*` and Phase 4 agent prepare/finalize IDs; requires all 99 aliases
- `packages/assurance-product/assurance_product/product-declaration-opencode.json`
- `packages/assurance-product/assurance_product/product-declaration-cursor.json`
- `tests/phase5/product_runner.py` — `run_to_terminal` / `resume`; `entrypoint` / `auto_archive` / `review_decision` / `healing_decision`
- `tests/phase5/runtime_composition.py` — second test plugin so Phase 4 operation IDs stay owner-prefixed
- `tests/phase5/graph_inventory.py` — allow Phase 4 operations; still forbid runtime and Phase 4 agent IDs
- `tests/phase5/test_graph_intake_and_triplets.py` — all 33 prepares / 99 aliases; new entrypoints
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

## Self-review

- YAML compiles under current engine kinds (`task`, `gate`, `join`, `subgraph`, `interrupt`, `end`). No `stop` node kind.
- Agent capabilities are only Task 1 aliases. Direct `runtime.*` and Phase 4 `assurance.improvement.*.prepare` / `.finalize` IDs remain forbidden by inventory/auth.
- `full` does not run Retro or Improvement review/apply. Archive is gated only by `auto_archive`.
- Resume rejects extra payload keys before the engine and rejects unknown actions through `EngineError`.
- Nested healing-disallowed STOP stays `stopped` with `has_nested_stop`.
- Registry/lock compares remain exact-99. Graph bindings are the 99 aliases plus six Phase 4 operations.
- Did not add `execute`, standalone `archive`, or issue-* public entrypoints (Task 18).

## Residuals

1. **`execute`, standalone `archive`, and `issue-review` / `issue-analyze` / `issue-reconcile` remain empty inventory slots.** Task 18 can attach them.
2. **Interrupt `reject` is a closed action but continues on the same edge as `approve`.** A later task can add an explicit reject STOP without changing the interrupt kind.
3. **Archive uses the agent triplet only.** Phase 4 `assurance.improvement.project-archive` is not on the graph; prepare/finalize carry the precheck/authority contract for this slice.
4. **Export uses `export-change-improvement` only.** `export-knowledge-improvement` is unused.
5. **`product_runner` uses a scripted in-process host**, not live OpenCode/Cursor, so activity-recovery and secret handles are not exercised here.
6. **`improvement-review` entrypoint is the review triplet itself** and has no interrupt; `needs-human` interrupt lives on `full` (after case-review) and `improvement-apply`.
