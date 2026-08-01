### Task 22: Activate the Current Scorer, Pending Datasets, and Hard Gates

**Files:**
- Modify: `assurance_agent/eval/scorers/codegen.py`
- Modify: `assurance_agent/eval/types.py`
- Modify: `eval/datasets/workflow-api-codegen/WAC-001.yaml`
- Modify: `eval/datasets/workflow-e2e-codegen/WEEC-001.yaml`
- Modify: `eval/datasets/workflow-fuzz-codegen/WFUZ-001.yaml`
- Modify: `eval/datasets/workflow-performance-codegen/WPER-001.yaml`
- Modify: `eval/suites/workflow-api-codegen.yaml`
- Modify: `eval/suites/workflow-e2e-codegen.yaml`
- Modify: `eval/suites/workflow-fuzz-codegen.yaml`
- Modify: `eval/suites/workflow-performance-codegen.yaml`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/eval/test_suite_load_all.py`
- Modify: `tests/unit/eval/test_fixtures.py`
- Modify: `tests/integration/test_eval_cli.py`

**Interfaces:**
- Produces: the single live switch from dormant current-chain calculations/pending tiers to four workflow-codegen datasets and suites that hard-gate the same three metrics.
- Consumes: green Task 20 real-runtime matrix, green Task 21 recovery matrix, Task 18 scorer, Task 19 validated pending tiers, and the strict Task 17 execution/evidence envelope.
- Preserves: complete L2/L3 run/full datasets, existing non-codegen scorer metrics, and the canonical one-time layer selection/policy replay boundary.

- [ ] **Step 1: Add failing atomic-activation assertions**

  Require each workflow-codegen dataset to reference exactly its `L2-*-codegen-pending` tier and each suite to register all three current metrics. For every metric require regression `{direction: higher_is_better, max_regression: 0.0}` and a hard threshold encoded in the real schema as `op: gte` plus `value: 1.0`. Removing or weakening any field fails loading; a deterministic result with one metric at `0.0` yields a failed verdict.
- [ ] **Step 2: Prove activation prerequisites are green**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_four_layer_resume.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_fixtures.py
  ```

  Expected: runtime, recovery, direct scorer, and dormant pending tiers pass before any live registration changes.
- [ ] **Step 3: Switch scorer, datasets, and suites in one diff**

  Register `current_assurance_chain_rate`, `current_codegen_attempt_rate`, and `selected_test_write_rate` in the live codegen scorer; switch all four datasets to their matching pending tier; add exact hard thresholds/regression policy to all four suites. Reject a staged set that changes only one of these three surfaces.
- [ ] **Step 4: Run policy replay and real CLI matrix**

  Through `tests/integration/test_eval_cli.py`, invoke real `aa eval run` command handling with the deterministic real-runtime adapter for one import-checkpoint sample per API/E2E/Fuzz/Performance suite, plus one no-tier/no-import fresh-root control. Assert the appropriate import/fresh root ID, list/scalar selection normalization, fixture validation, runtime params, D17 policy, execution envelope, exported evidence, live metric keys, and final verdict all agree. Re-run the fifteen selection-subset policy/scorer table and one tampered-policy negative; no OpenCode server is used.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/integration/test_eval_cli.py -k codegen
  uv run ruff check assurance_agent/eval/scorers/codegen.py assurance_agent/eval/types.py tests/unit/eval tests/integration/test_eval_cli.py
  uv run pyright
  ```

  Expected: all four live suites consume only pending inputs and fail unless all three replayed current-chain metrics equal `1.0`.
- [ ] **Step 6: Commit benchmark activation**

  ```bash
  git add assurance_agent/eval/scorers/codegen.py assurance_agent/eval/types.py \
    eval/datasets/workflow-api-codegen/WAC-001.yaml \
    eval/datasets/workflow-e2e-codegen/WEEC-001.yaml \
    eval/datasets/workflow-fuzz-codegen/WFUZ-001.yaml \
    eval/datasets/workflow-performance-codegen/WPER-001.yaml \
    eval/suites/workflow-api-codegen.yaml \
    eval/suites/workflow-e2e-codegen.yaml \
    eval/suites/workflow-fuzz-codegen.yaml \
    eval/suites/workflow-performance-codegen.yaml \
    tests/unit/eval/test_scorers.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/unit/eval/test_fixtures.py \
    tests/integration/test_eval_cli.py
  git commit -m "feat(eval): activate four-layer evidence gates"
  ```

