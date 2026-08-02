# Task 20 Report — Real Four-by-Two and Multi-Layer GraphRuntime Matrix

## Status

**DONE** (edit + test only; no git add/commit — controller owns commits)

## Files Changed

```
tests/helpers_four_layer_runtime.py                          (create)
tests/integration/test_four_layer_codegen_only.py            (create)
tests/integration/test_api_e2e_assurance_flow.py             (modify)
tests/integration/test_fuzz_performance_assurance_flow.py    (modify)
assurance_agent/workflow/graph/scheduler.py                  (modify)
tests/unit/workflow/graph/test_scheduler.py                  (modify)
.superpowers/sdd/task-20-report.md
```

Listed brief targets `replay_binding.py`, `runtime.py`, `precommit.py`, and `task_inputs.py` needed no production edits for the matrix.

## What Landed

1. **Deterministic adapter + coordinator seams** — target/attempt-keyed agent output, named malformed/missing/forbidden mutations, request recording, separate `CoordinatorFaultInjector`, and test-only `BarrierNodeRunner`.
2. **Packaged runtime fixture** — seeds canonical assurance contracts, bootstrap import checkpoint, synthetic `fixture-lock.json`, and `codegen-only` execute helper via `build_graph_runtime`.
3. **Eight base cells** — applicable + inapplicable for api/e2e/fuzz/performance; applicability-error control (malformed case does not become skip).
4. **Stale/negative controls** — pass-shaped stale review/checks/summary/manifest cannot authorize commit; byte-identical rewrite still requires fresh attempt ownership.
5. **Multi-layer + join** — API+E2E / mixed / all-four / single-selected; join waits on held codegen and on inapplicable gate barrier; no `operation:run-tests` in codegen-only.
6. **Production fix** — child contexts bind `project_root` to the attempt workspace while `change_dir` stays on the host ledger; `_current_change_repo_path()` prefers `write_set.base_tree_roots["change"]`, then resolved `relative_to`, then `qa/changes/{change_id}` (also tolerates macOS `/var` vs `/private/var`).
7. **Smoke upgrades** — synthetic api/e2e/fuzz/performance flow tasks set `outputs_committed=True` and exercise real-runtime smoke paths.

## Verification

```text
uv run pytest -q \
  tests/integration/test_four_layer_codegen_only.py \
  tests/integration/test_api_e2e_assurance_flow.py \
  tests/integration/test_fuzz_performance_assurance_flow.py \
  tests/unit/verification/test_assurance_contract_mutations.py \
  tests/unit/workflow/graph/test_assurance_topology_mutations.py
→ 122 passed

uv run ruff check tests/helpers_four_layer_runtime.py \
  tests/integration/test_four_layer_codegen_only.py \
  assurance_agent/workflow/graph/replay_binding.py \
  assurance_agent/workflow/graph/runtime.py \
  assurance_agent/workflow/graph/scheduler.py \
  assurance_agent/workflow/graph/precommit.py \
  assurance_agent/workflow/graph/task_inputs.py
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations
```

## Preserved

- Adapter supplies agent output only; coordinator faults are separate seams.
- No plan or run-tests dispatch in codegen-only cells.
- Cursor-loop files untouched.
- No git add/commit.

## Suggested Commit (controller)

```text
test(runtime): prove four-layer codegen-only execution
```
