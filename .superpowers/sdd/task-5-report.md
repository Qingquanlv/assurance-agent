# Task 5 Report: Product wiring (thin `init`, `full`, `intake`)

## Status

DONE_WITH_CONCERNS

## TDD Evidence

The brief is implement-then-verify (constructor sites, `adapt_init` / `route_init` / roots, then the listed pytest). No separate RED flip was required.

### GREEN (Step 4)

Command:

```bash
uv run pytest tests/product/test_feature_graph_bundles.py tests/product/test_product_entrypoints.py tests/product/test_graph_revision_contracts.py tests/product/test_cli_langgraph_lifecycle.py tests/product/test_product_stategraph_flow.py tests/product/test_stategraph_entrypoints.py tests/product/test_full_graph_audit.py -v
```

Output:

```
======================== 77 passed in 219.83s (0:03:39) ========================
```

Exit code: 0

Focused ruff check/format on the edited graph, factory, runtime-count, and listed test files is clean.

First run was `75 passed, 2 errors`: `opencode_composition` raised `semantic attempt registry must contain 46 contracts, got 47`. Those two listed tests (`test_non_agent_root_survives_reopen_status_lock_resume_and_publication`, `test_factory_composition_has_no_leftover_compiled_workflow`) passed after the in-scope `46` → `47` count updates.

## What changed

- Thin `init` root is `compile_thin_root(..., entrypoint="init", adapt=adapt_init)`.
- `adapt_init` puts `data_knowledge` on state as `payload.data_knowledge.model_dump(mode="json")` so `select_init_runtime` can read `state["data_knowledge"]`.
- `route_init` returns `initialized` | `failed` (`attempt_failure` or `status == "failed"`).
- `full` and `intake` always run `adapt-init` → `init` after prepare succeeds. Prepare `failed` still goes to `not-achieved` / `publish`. `advance-coverage` still edges only to `adapt-case` (does not re-run init).
- Factory builds 13 thin roots and 15 product roots. `PUBLIC_BUNDLE_FIELDS["assurance.generation"]` stays `("generation",)`; `IMPLEMENTED_BUNDLE_FIELDS` appends `init_runtime`.
- Every listed `GenerationGraphs(...)` now passes `init_runtime` last.

## Files Changed

| File | Action |
|------|--------|
| `assurance_product/graphs/entrypoints.py` | `adapt_init`, `build_init_root`; intake always inits after prepare |
| `assurance_product/graphs/routes.py` | `route_init` → `initialized` \| `failed` |
| `assurance_product/graphs/full.py` | `adapt-init` / `init` after prepare; coverage reentry skips init |
| `assurance_product/graphs/factory.py` | `"init"` thin root; intake gets `init_runtime`; 13 / 15 counts |
| `assurance_product/runtime_bindings.py` | semantic registry count 46 → 47 |
| `assurance_product/runtime_ports.py` | composition semantic count 46 → 47 |
| `tests/product/test_feature_graph_bundles.py` | `IMPLEMENTED_BUNDLE_FIELDS` appends `init_runtime` |
| `tests/product/test_stategraph_entrypoints.py` | `_stub_features` `init_runtime`; thin count 13 |
| `tests/product/test_product_stategraph_flow.py` | `_flow_features` `init_runtime`; product count 15; full nodes include `adapt-init` / `init` |
| `tests/product/goal_loop_fixture.py` | `GenerationGraphs` `init_runtime` last |
| `tests/product/test_full_graph_audit.py` | `ENTRYPOINT_CONTRACTS` length 14 → 15 |

## Commit

none

## Concerns

1. **`runtime_bindings.py` / `runtime_ports.py` were not in the brief file list.** Listed composition tests failed on the leftover `46` semantic-contract check after `init-test-runtime`. Updated those two asserts to `47` as the brief allows. Other `== 46` tests (`test_semantic_attempt_bindings.py`, `test_product_composition.py`, `test_python_native_cutover.py`) were not run.
2. **`test_full_graph_audit.py` was not in the brief file list** but is in the pytest command. Its `ENTRYPOINT_CONTRACTS` length was still `14`; updated to `15`.
3. **Stale test names** still say “twelve thin roots” (`test_twelve_thin_roots_...`, `test_build_product_graphs_merges_twelve_thin_roots_...`). Counts in those tests are 13 / 15.
4. **`route_init` is a plain `if`, not an exclusive-route row.** It is not in `PRODUCT_EXCLUSIVE_ROUTES`. The existing `if`/`orelse` AST guard still passes.
5. **`init` is not in `_THIN_EXPORTS` / representative thin-root invoke tests.** Wiring is covered by factory compile, full-node presence, and intake/full flow stubs.
