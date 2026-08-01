### Task 18: Dark-Ship Selected-Test Behavior and Current-Chain Scoring

**Files:**
- Modify: `assurance_agent/verification/generated_entries.py`
- Create: `assurance_agent/eval/scorers/current_codegen.py`
- Modify: `assurance_agent/eval/scorers/shared.py`
- Modify: `tests/unit/verification/test_generated_entries.py`
- Create: `tests/unit/eval/test_codegen_scorer.py`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/verification/test_fuzz_performance_contract_fixtures.py`
- Modify: `tests/unit/workflow/graph/test_task_input_snapshot.py`

**Interfaces:**
- Produces: typed plan mapping extraction, `GeneratedEntryDecision`, four AST classifiers, current root/branch/cycle attempt binding, `CurrentLayerCodegenEvidence`, and callable calculations for the three future hard metrics.
- Consumes: verified execution/D17/policy/export evidence, pinned historical role manifest, epoch-closed ledger attempts, D16 manifest/receipt/snapshot/write set/blobs, content diff, plan/case mappings, and selected layers.
- Preserves: the live `eval/scorers/codegen.py` registration and existing syntax/secret/summary/evidence verdicts. This task is dark: no dataset or suite can request the three new metrics until Task 22 activates them after runtime/recovery coverage.

- [ ] **Step 1: Extend the strict mappings with behavioral classification inputs**

  Reuse Task 6's strict Fuzz Test Function Mapping/Schema Acquisition and Performance Task Mapping/Target File records. Extend those typed mapping objects with the closed behavioral policy inputs required by the scorer; retain fail-closed missing/duplicate/interrupted/malformed behavior and do not change the canonical plan wire shape.
- [ ] **Step 2: Add independent positive/negative AST corpora**

  API requires a mapped `test_*` request through a declared client and a non-constant response-dependent assertion. E2E requires navigation, interaction, and page/locator-dependent expect/assert. Fuzz requires bound schema/parametrize, generated case consumption, and `call_and_validate` or one registered equivalent. Performance requires a mapped method on `HttpUser`/`FastHttpUser`, `@task`, and `self.client` request in that method.
- [ ] **Step 3: Reject pass-shaped but behaviorless code**

  Parameterize assignment-only, bare return, `assert True`, constant comparison, decorator-only function, helper-only file, unmapped symbol, wrong case, API request without dependent assertion, E2E navigation without interaction/assertion, Fuzz without bound schema call, and Locust task without request or outside a User subclass.
- [ ] **Step 4: Add epoch-closed current-chain tests**

  For each selected layer, bind root, assurance child, branch, cycle generation, trees, applicability/preflight, reviewer, mechanical, gate, precheck, and codegen. Applicable requires the full chain and no plan attempt in codegen-only; inapplicable requires current N/A/skip evidence and absence of reviewer/codegen. Mixing byte-identical evidence from two cycles must fail.
- [ ] **Step 5: Add manifest/receipt/write attribution tests**

  Require current summary+manifest output digests, successful `generated_files_candidate/v1` receipt, input snapshot, non-empty write set, selected private-root `test_entry` add/content-modify, after/blob/final-manifest digest equality, content-diff attribution, zero sibling writes, and zero forbidden policy writes. Delete/chmod/shared/support/summary-only/pre-existing test cases score zero.
- [ ] **Step 6: Lock aggregation semantics**

  `current_assurance_chain_rate` divides by every selected layer. `current_codegen_attempt_rate` and `selected_test_write_rate` divide by selected applicable layers and return `0.0` when none apply. Cover one applicable+one inapplicable, two applicable with one write, and a successful branch from another root.
- [ ] **Step 7: Run tests and observe presence/syntax false positives**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  ```

  Expected: current scorer gives credit to pre-existing/`assert True` syntax and lacks lineage/receipt checks.
- [ ] **Step 8: Implement shared mappings/classifiers and current evidence binding**

  Load plan/case bytes only from the attempt-bound snapshot/exported blobs. Use the shared layer registry and physical resolver. Extend the D13 field-consumer inventory with scorer snapshot/context reads. Keep registered client/schema-call forms closed policy data with direct mutation coverage; do not match arbitrary call-name substrings.
- [ ] **Step 9: Replay policy before scoring writes**

  Strictly reconstruct D17 location and `WritePolicyV1` from canonical selected layers. Require byte-identical persisted policy before classifying any write. Any evidence/policy mismatch sets all three hard metrics to zero.
- [ ] **Step 10: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  uv run ruff check assurance_agent/verification/generated_entries.py assurance_agent/eval/scorers tests/unit/verification/test_generated_entries.py tests/unit/eval/test_codegen_scorer.py
  uv run pyright
  ```

  Expected: direct calls credit only current, mapped, behaviorally meaningful, receipt-bound selected test writes; the live scorer still exposes no new hard metric.
- [ ] **Step 11: Commit the dark current-chain scorer**

  ```bash
  git add assurance_agent/verification/generated_entries.py \
    assurance_agent/eval/scorers/current_codegen.py \
    assurance_agent/eval/scorers/shared.py \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  git commit -m "feat(eval): stage current behavioral codegen evidence"
  ```

