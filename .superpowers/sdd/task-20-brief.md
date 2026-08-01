### Task 20: Drive the Real Four-by-Two and Multi-Layer GraphRuntime Matrix

**Files:**
- Create: `tests/helpers_four_layer_runtime.py`
- Create: `tests/integration/test_four_layer_codegen_only.py`
- Modify: `tests/integration/test_api_e2e_assurance_flow.py`
- Modify: `tests/integration/test_fuzz_performance_assurance_flow.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/graph/precommit.py`
- Modify: `assurance_agent/workflow/graph/task_inputs.py`

**Interfaces:**
- Produces: a deterministic target/attempt-aware adapter, coordinator-only snapshot fault injector, test-only `BarrierNodeRunner`, real packaged runtime fixture, eight base cells, stale-evidence cases, and representative multi-layer/join coverage.
- Consumes: `build_graph_runtime`, the production import-checkpoint path, packaged schema/contracts/catalog/personas, real journal/tree/workspace/freeze/apply/child invocation pipeline, and canonical layer outputs.
- Preserves: the adapter supplies agent output only; coordinator evidence faults are separate test seams. Neither helper implements gates, topology, planner decisions, or write authorization.

- [ ] **Step 1: Build a deterministic adapter keyed by exact target and attempt**

  It writes valid strict outputs to the provided attempt workspace and can inject one named malformed/missing/forbidden output mutation. Record every target/persona/workspace request so tests can prove no plan dispatch in codegen-only and no unselected branch dispatch. Put snapshot-CAS/start-reference corruption behind a separate coordinator fault injector because an agent cannot mutate that authority.
- [ ] **Step 2: Add the four applicable base cells**

  For API/E2E/Fuzz/Performance individually, use the production import-checkpoint path to import only bootstrap/plan-ready predecessors; selected branch/reviewer/mechanical/gate/precheck/codegen/join roles remain pending. Then run `execute` in `codegen-only` mode with `run_tests=false`. Assert current applicability, reviewer, mechanical, gate, precheck, and codegen attempts; Fuzz/Performance parent preflight; no plan or `operation:run-tests` attempt; summary+manifest+receipt+snapshot+write set; correct private test write; and completed branch/join-to-END.
- [ ] **Step 3: Add the four inapplicable base cells**

  Assert current applicability, four N/A checks, skipped gate/precheck, no reviewer/codegen attempt, no generated output/write, and completed branch/join. A valid case set with only another layer or no selected automated case is N/A. Malformed case YAML or non-boolean `automation.required` is an applicability error; wrong/missing/malformed N/A stops rather than becoming skip.
- [ ] **Step 4: Add stale/pre-existing artifact cases**

  Seed pass-shaped old review/check/summary/test bytes from another root/cycle and assert they cannot replace current producer attempts. Cover reviewer success with no review output; mechanical failure/no committed checks despite stale checks; missing manifest; shape-valid manifest/write-set mismatch; forged selected-role `task_imported`; and coordinator-injected stale snapshot/reference. Each fails at its named ingest/precommit/precheck owner with no commit. The positive control has each current producer rewrite byte-identical canonical bytes and passes because attempt/snapshot/receipt/write-set ownership is fresh.
- [ ] **Step 5: Add representative multi-layer selections**

  Cover default API+E2E, API+Fuzz+Performance with mixed applicable/inapplicable layers, all four applicable, and one selected layer with all siblings unselected. Assert unselected absence, one canonical params tuple/policy, and each active branch's complete chain. Hold the last applicable codegen with a blocking adapter; separately hold an inapplicable gate result before commit with test-only `BarrierNodeRunner`. In both cases `generation-join` must wait, then start exactly once after release, and no execution successor runs.
- [ ] **Step 6: Run tests and observe any runtime-only seams**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py
  ```

  Expected: every failing assertion names a production boundary; no helper fallback is permitted.
- [ ] **Step 7: Apply only owner-local production fixes exposed by the real matrix**

  Add each discovered case to its owner-specific mutation suite before fixing it, then change the actual owner (`runtime`, replay binding, scheduler, snapshot, or precommit) rather than the harness. If the owner is a Task 10 semantic definition, move the correction into the pre-freeze task or add a formally versioned manifest/dispatcher; never silently change v1 bytes. Do not add target-specific scheduler dispatch or generate topology in Python.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py
  uv run ruff check tests/helpers_four_layer_runtime.py tests/integration/test_four_layer_codegen_only.py assurance_agent/workflow/graph/replay_binding.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/scheduler.py assurance_agent/workflow/graph/precommit.py assurance_agent/workflow/graph/task_inputs.py
  uv run pyright
  ```

  Expected: all eight cells and three multi-layer controls pass through the real runtime.
- [ ] **Step 9: Commit the real runtime matrix and necessary fixes**

  ```bash
  git add tests/helpers_four_layer_runtime.py \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/scheduler.py \
    assurance_agent/workflow/graph/precommit.py \
    assurance_agent/workflow/graph/task_inputs.py
  git commit -m "test(runtime): prove four-layer codegen-only execution"
  ```

