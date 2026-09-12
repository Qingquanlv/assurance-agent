# Task 3 Report: Generation subgraph `init_runtime`

## Status

DONE_WITH_CONCERNS

## TDD Evidence

### RED (Step 2)

Factory field tuple and bound-contract counts were updated first. `test_init_runtime_graph.py` was added. Production `GenerationGraphs` still had five fields and did not bind `assurance.generation.init-test-runtime`.

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests/test_generation_graph_factory.py::test_generation_factory_exports_root_and_four_families packages/capabilities/assurance-generation/tests/test_init_runtime_graph.py -v
```

Output:

```
FAILED test_generation_factory_exports_root_and_four_families
  AssertionError: assert ('generation', 'api', 'e2e', 'fuzz', 'performance')
                   == ('generation', 'api', 'e2e', 'fuzz', 'performance', 'init_runtime')

FAILED test_init_runtime_is_a_one_attempt_graph
  AttributeError: 'GenerationGraphs' object has no attribute 'init_runtime'
```

Exit code: 1. Failures matched the missing field / missing subgraph, not typos.

### GREEN (Step 4)

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests/test_generation_graph_factory.py packages/capabilities/assurance-generation/tests/test_init_runtime_graph.py -v
```

Output:

```
============================== 20 passed in 1.91s ==============================
```

Exit code: 0

Focused ruff on the new/changed graph files and `test_init_runtime_graph.py` is clean. `test_generation_graph_factory.py` already has a pre-existing `ruff format` wrap on `allowed_artifact_paths`; that line was not reformatted.

## What changed

- `GenerationGraphs.init_runtime` is the last dataclass field, after `performance`.
- The subgraph is a one-attempt graph: `generation.init-test-runtime` binds contract id `assurance.generation.init-test-runtime` (not the handler id).
- `select_init_runtime` / `activation_init_runtime` / `publish_init_runtime` live in `nodes.py` with the brief names and shapes.
- Local `terminal_done` in `init_runtime.py` sets `status` to `completed` or `failed`. It does not call `generation_done` (that requires `GenerationCycleResultV1`) and does not reuse family `nodes.terminal_done` (that sets `passed`).
- `route_attempt_result` maps `committed` → `done` and `failed` → `failed`.
- The family generation root (`_build_root_graph`) was not changed.
- `GenerationState` gained optional `data_knowledge` and `init_result`.

## Files Changed

| File | Action |
|------|--------|
| `assurance_generation/graphs/init_runtime.py` | Created one-attempt subgraph |
| `assurance_generation/graphs/nodes.py` | `select_init_runtime` / `activation_init_runtime` / `publish_init_runtime` |
| `assurance_generation/graphs/factory.py` | `init_runtime` field + `build_init_runtime_graph` |
| `assurance_generation/graphs/state.py` | optional `data_knowledge`, `init_result` |
| `tests/test_init_runtime_graph.py` | Created; duplicates `recording_context` via `generation_contracts` |
| `tests/test_generation_graph_factory.py` | field tuple + bound count 14 → 15 |

## Commit

none

## Concerns

1. **Product constructors will break until Task 5.** `GenerationGraphs(...)` is constructed without `init_runtime` in `tests/product/test_stategraph_entrypoints.py`, `tests/product/test_product_stategraph_flow.py`, and `tests/product/goal_loop_fixture.py`. The plan already schedules those updates.
2. **`publish_init_runtime` writes `receipts`, but `GenerationState` does not declare that field.** The brief only asked for optional `data_knowledge` / `init_result`. LangGraph may drop undeclared `receipts` on this subgraph state.
3. **`GENERATION_GRAPH_CONTRACT_IDS` still omits `assurance.generation.init-test-runtime`.** That tuple lives in `attempts.py`, which is outside this task’s file list (Task 2 leftover).
4. **No invoke/script test** of the subgraph — only node presence and factory binding counts, as specified.
