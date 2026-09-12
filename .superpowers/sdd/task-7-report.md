# Task 7 Report: Repo gate for the touched wheels

## Status

DONE

## TDD Evidence

The brief is implement-then-verify (focused ruff / pyright / pytest, then leftover init counts). No separate RED flip was required.

### Step 1: Format / lint

Command:

```bash
uv run ruff check packages/capabilities/assurance-generation packages/products/assurance-product benchmark/assurance-product/run_item.py tests/product && uv run ruff format packages/capabilities/assurance-generation packages/products/assurance-product benchmark/assurance-product/run_item.py tests/product
```

First run output:

```
All checks passed!
10 files reformatted, 198 files left unchanged
```

Exit code: 0

Re-run after count/declaration edits:

```bash
uv run ruff check packages/capabilities/assurance-generation packages/products/assurance-product benchmark/assurance-product/run_item.py tests/product && uv run ruff format --check packages/capabilities/assurance-generation packages/products/assurance-product benchmark/assurance-product/run_item.py tests/product
```

Output:

```
All checks passed!
208 files already formatted
```

Exit code: 0

### Step 2: Typecheck

Command:

```bash
uv run pyright packages/capabilities/assurance-generation/assurance_generation packages/products/assurance-product/assurance_product
```

Output:

```
0 errors, 1 warning, 0 informations
```

Exit code: 0

The single warning is pre-existing `reportMissingModuleSource` for `jsonschema` in `assurance_generation/resources/test-runtime/tests/schema_validation.py` (wheel harness template). No flood of include-override errors, so repo-root `uv run pyright` was not needed.

### Step 3: Focused pytest

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests/test_init_runtime.py packages/capabilities/assurance-generation/tests/test_init_runtime_graph.py packages/capabilities/assurance-generation/tests/test_generation_graph_factory.py packages/capabilities/assurance-generation/tests/test_plugin.py tests/product/test_product_input.py tests/product/test_product_entrypoints.py tests/product/test_feature_graph_bundles.py tests/product/test_product_stategraph_flow.py tests/product/test_phase5_benchmark_change_layout.py -v
```

Output (after leftover fixes):

```
============================= 142 passed in 18.15s =============================
```

Exit code: 0

Did not run or “fix” `packages/capabilities/assurance-generation/tests/test_plan_consistency.py::test_check_collects_cross_artifact_contradictions_in_one_pass`. Did not start a live `aa run`.

### Extra leftover verification (not in the brief list)

Command:

```bash
uv run pytest tests/product/test_semantic_attempt_bindings.py tests/product/test_product_composition.py tests/product/test_python_native_cutover.py tests/product/test_wheel_smoke_contract.py tests/product/test_stategraph_entrypoints.py -q
```

Output:

```
72 passed in 21.07s
```

Exit code: 0

`tests/product/test_product_providers.py` (declaration bytes) also passed after regenerating the committed product declaration.

## What changed

- Closed semantic-attempt registry asserts are `47` (32 agent + 15 task). Task count asserts are `15`. Public root asserts are `15`.
- `GENERATION_GRAPH_CONTRACT_IDS` now includes `assurance.generation.init-test-runtime`.
- Committed `product-declaration-opencode.json` now lists `init` (regenerated via `write_committed_product_declarations()`). Composition was still serving the 14-root declaration.
- Stale “twelve thin roots” test names are “thirteen”. `_THIN_EXPORTS` includes `init` → `assurance.generation.init_runtime`; `_CHILD_STATE` includes `GenerationState`.
- Smoke script and its contract test expect `15 roots` and `47` attempt contracts.

## Files Changed

| File | Action |
|------|--------|
| `assurance_generation/contracts/attempts.py` | Append `init-test-runtime` to `GENERATION_GRAPH_CONTRACT_IDS` |
| `assurance_product/product-declaration-opencode.json` | Regenerated; `init` is a public root |
| `scripts/assurance_product_wheel_smoke_test.sh` | 14→15 roots; 46→47 attempt contracts |
| `tests/product/test_semantic_attempt_bindings.py` | Registry 46→47; tasks 14→15 |
| `tests/product/test_product_composition.py` | Attempt sum 46→47; entrypoints 14→15 |
| `tests/product/test_python_native_cutover.py` | Boot contracts 46→47 |
| `tests/product/test_wheel_smoke_contract.py` | Expect 15 roots / 47 in the smoke script |
| `tests/product/test_stategraph_entrypoints.py` | `_THIN_EXPORTS` + `init`; rename twelve→thirteen |
| `tests/product/test_product_stategraph_flow.py` | Rename twelve→thirteen |

First `ruff format` also reformatted 10 already-scoped files (style only).

## Commit

none

## Concerns

1. Scoped pyright reports one pre-existing warning (`jsonschema` missing in the harness template). Zero errors.
2. `init` is in `_THIN_EXPORTS` but still not in `_REPRESENTATIVE_THIN_ENTRYPOINTS` invoke tests. Wiring stays covered by factory compile and intake/full stubs.

## Final-review fix

Coverage re-entry now counts `init` the same way as prepare/case (`{"prepare": 1, "init": 1, "case": 2}`). Added `test_full_init_failure_does_not_enter_case`: init stub returns `status="failed"` plus `attempt_failure`; case is never called; full terminal is `{"status": "failed", "reason": "not_achieved"}`. No production wiring change.

Command:

```bash
uv run pytest tests/product/test_product_stategraph_flow.py -v
```

Output:

```
============================== 23 passed in 2.04s ==============================
```

Exit code: 0

Files changed: `tests/product/test_product_stategraph_flow.py` only.
