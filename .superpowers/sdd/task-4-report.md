# Task 4 Report: Product inventory (`init` is a public name)

## Status

DONE_WITH_CONCERNS

## TDD Evidence

### RED (Step 2)

Test inventories were flipped first. Production `FAMILY_EMPTY_ENTRYPOINTS` still omitted `init`.

Command:

```bash
uv run pytest tests/product/test_product_input.py tests/product/test_product_entrypoints.py tests/product/test_graph_intake_and_triplets.py -v
```

Output:

```
FAILED test_empty_family_entrypoints_require_empty_selection[init]
  ValueError: unknown product entrypoint: init

FAILED test_product_entrypoints_are_fifteen_python_roots
  Extra items in the right set: 'init'

FAILED test_public_entrypoints_are_the_python_product_roots
  Extra items in the right set: 'init'

ERROR test_product_input_authenticates_resource_refs_against_composition
  ValueError: semantic attempt registry must contain 46 contracts, got 47
```

Exit code: 1. Inventory failures matched the missing public name, not typos.

### GREEN (Step 4)

Command:

```bash
uv run pytest tests/product/test_product_input.py tests/product/test_product_entrypoints.py tests/product/test_graph_intake_and_triplets.py -v
```

Output:

```
========================= 56 passed, 1 error in 11.84s =========================
```

`validate_for_entrypoint("init")` passed (empty families, no `case_delta`, no `plan_ref`, no `retro_window`). Set-membership tests assert 15 public names and 13 thin roots. Focused ruff on the eight listed files is clean.

The remaining ERROR is the pre-existing Task 3 leftover `46` vs `47` semantic-attempt count during `opencode_composition` setup. It is outside this task’s file list.

`test_graph_revision_contracts.py` and `test_cli_langgraph_lifecycle.py` were updated but not executed: the former compiles thin roots; the latter is a lifecycle suite. Factory compile tests were not run (Task 5).

## What changed

- `init` is a public `FAMILY_EMPTY` name: no families, no `case_delta`, no `plan_ref`, no `retro_window`.
- Agent contracts are `()`. Recursion limit is `512`.
- Closed-set counts are 15 public / 13 thin. Factory still builds 12 thin roots until Task 5.
- Renamed set-membership tests from fourteen → fifteen. `init` is in `_NON_AGENT_ENTRYPOINTS`.

## Files Changed

| File | Action |
|------|--------|
| `assurance_product/models.py` | `"init"` in `FAMILY_EMPTY_ENTRYPOINTS` |
| `assurance_product/application.py` | `"init": ()`; error text `15 public names` |
| `assurance_product/graphs/revisions.py` | `"init": 512` |
| `tests/product/test_product_input.py` | `"init"` in `_FAMILY_EMPTY_ENTRYPOINTS` |
| `tests/product/test_product_entrypoints.py` | `"init"` in `PUBLIC_ENTRYPOINTS`; `len == 15` / thin `13`; renamed `...fifteen...` |
| `tests/product/test_graph_intake_and_triplets.py` | `"init"`; `len == 15` |
| `tests/product/test_cli_langgraph_lifecycle.py` | `"init"` in `_NON_AGENT_ENTRYPOINTS`; `len == 15`; renamed `test_all_fifteen_...` |
| `tests/product/test_graph_revision_contracts.py` | `"init": 512`; thin length 13 |

## Commit

none

## Concerns

1. **`test_product_input_authenticates_resource_refs_against_composition` still ERRORs** on `semantic attempt registry must contain 46 contracts, got 47`. This is the Task 3 `init_runtime` contract leftover, not an inventory miss. It failed the same way on RED before production edits.
2. **Factory compile tests will fail until Task 5.** `build_thin_entrypoint_graphs` still requires 12 thin roots. `test_graph_revision_contracts.py` now expects thin length 13 and was not run.
3. **Out-of-list files still say 14** (`test_product_composition.py`, `test_full_graph_audit.py`, `public_closure.py`, and others). Left as-is.
