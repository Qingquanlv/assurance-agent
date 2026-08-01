### Task 19: Prepare Truthful Codegen-Pending Tiers Without Activating Them

**Files:**
- Modify: `assurance_agent/eval/fixtures.py`
- Modify: `assurance_agent/eval/types.py`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L1-assurance-input-ready.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-pending.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/config.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/api.py`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/e2e/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review-summary.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-checks.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review-summary.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-checks.json`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json`
- Modify: `tests/unit/eval/test_fixtures.py`

**Interfaces:**
- Produces: `TierManifest.repo_paths`, `TierManifest.expected_layers`, selected-role-aware full-ancestry validation, one independent common base, and four dormant plan-ready/codegen-pending tiers.
- Consumes: locked benchmark fixture paths/digests, canonical selected layers, truthful L1 domain capability, exact current plan/case inputs, and the structured historical-role vocabulary.
- Preserves: current workflow-codegen dataset/suite references and live scorer registration; existing complete L2/L3 tiers remain importable after the dynamic review/check seeder is removed.

- [ ] **Step 1: Add full-ancestry pending-tier rejection tests**

  Reject every selected assurance-chain role by structured role identity: applicability, branch wrapper, review-cycle wrapper, reviewer, mechanical, gate, precheck, codegen, and generation-join. Also reject review/check JSON, codegen summary/manifest, mapped target test/stub, and resets that mark selected work done. Cover every role/artifact directly and inherited from a parent; permit only declared config, adapter, conftest, and reusable support.
- [ ] **Step 2: Add locked `repo_paths` safety and capability tests**

  Copy `.aa/config.yaml`, `.aa/data-knowledge.yaml`, and reusable support only from `fixture-lock.json` into the isolated attempt SUT. Reject absolute/parent/symlink paths and digest mismatch. Make `tests/testdata/domain/api.py` expose the real capability with product dependencies imported lazily inside the factory, so module and symbol import smoke passes in the isolated fixture. If lazy import is impossible, lock the smallest real dependency closure; never install fake `app.*` modules.
- [ ] **Step 3: Freeze complete-tier review/check evidence, then remove dynamic seeding**

  Add and lock the six exact Fuzz/Performance review/check files above, import them from their existing complete L2 tiers, and seed every old L2/L3 tier successfully. Only then delete the exact `_ensure_assurance_seed_artifacts(...)` helper. `expected_layers` remains validation metadata and never supplies params or resets.
- [ ] **Step 4: Add independent pending tiers and repair source mappings**

  Build `L1-assurance-input-ready` without complete-tier ancestry and add four child pending tiers. Repair Fuzz/Performance case and plan mappings to the real API entity/capability. Assert expanded pending chains contain no selected completion or mapped target fallback while complete chains retain summaries, manifests, tests, and frozen reviews/checks. Do not switch a dataset or suite yet.
- [ ] **Step 5: Run tests and observe current helper/ancestry gaps**

  ```bash
  uv run pytest -q tests/unit/eval/test_fixtures.py
  ```

  Expected: the current fixture implementation still relies on dynamic assurance seeding and cannot reject hidden selected wrappers/artifacts across ancestry.
- [ ] **Step 6: Implement metadata, role-aware validation, and locked copying**

  Expand the complete `extends` chain before copying/importing. Validate `paths`, `repo_paths`, imports, and resets against the one resolved selected tuple and structured role map. Recompute `fixture-lock.json` with the repository helper, never by manual digest editing.
- [ ] **Step 7: Run focused and static gates**

  ```bash
  uv run pytest -q tests/unit/eval/test_fixtures.py
  uv run ruff check assurance_agent/eval/fixtures.py assurance_agent/eval/types.py tests/unit/eval/test_fixtures.py
  uv run pyright
  ```

  Expected: all four dormant pending tiers validate and seed as plan-ready/codegen-pending; every existing complete L2/L3 tier preserves complete import behavior without a dynamic helper.
- [ ] **Step 8: Commit dormant truthful fixture inputs**

  ```bash
  git add assurance_agent/eval/fixtures.py assurance_agent/eval/types.py \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L1-assurance-input-ready.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/config.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/api.py \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/e2e/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review-summary.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-checks.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review-summary.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-checks.json \
    benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json \
    tests/unit/eval/test_fixtures.py
  git commit -m "test(eval): stage truthful codegen-pending fixtures"
  ```

