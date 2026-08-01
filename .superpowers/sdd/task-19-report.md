# Task 19 Report — Prepare Truthful Codegen-Pending Tiers Without Activating Them

## Status

**DONE** (edit + test only; no git add/commit — controller owns commits)

## Files Changed

```
assurance_agent/eval/fixtures.py
assurance_agent/eval/types.py
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L1-assurance-input-ready.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-pending.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-pending.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-pending.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-pending.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml
benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/config.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/api.py
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/__init__.py
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/e2e/case.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review-summary.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-checks.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review-summary.md
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-checks.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/fuzz/test_api_fuzz.py
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/perf/locustfile_api.py
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/fuzz-generated-files.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/performance-generated-files.json
benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json
tests/unit/eval/test_fixtures.py
.superpowers/sdd/task-19-report.md
```

## What Landed

1. **`TierManifest.repo_paths` / `expected_layers`** — fixture models moved to `eval/types.py`; `load_tier` merges `repo_paths` and keeps `expected_layers` as child-only validation metadata.
2. **Full-ancestry pending validation** — `validate_tier_for_selection` expands the `extends` chain and rejects selected assurance roles (applicability/review/mechanical/gate/wrappers/precheck/codegen/branch), review/check/codegen artifacts, mapped targets, and selected codegen-done resets.
3. **Locked SUT copies** — `seed_change` copies `repo_paths` into the isolated attempt SUT only; rejects absolute/parent/symlink paths; digests verified via `fixture-lock.json` (recomputed with `write_fixture_lock`).
4. **Truthful L1 capability** — `.aa/config.yaml`, `.aa/data-knowledge.yaml`, and lazy-import `tests/testdata/domain/api.py` (`make_api` / `cleanup_api` / `list_auth_routes`) with product `app.*` imports inside factories.
5. **Frozen complete-tier reviews/checks** — six Fuzz/Performance review/check files frozen after repairing plans/cases to the API entity; `_ensure_assurance_seed_artifacts` removed.
6. **Dormant pending tiers** — independent `L1-assurance-input-ready` + four `L2-*-codegen-pending` children. Datasets/suites still point at complete seeds (Task 22 activation).

## Verification

```text
uv run pytest -q tests/unit/eval/test_fixtures.py
→ 34 passed

uv run ruff check assurance_agent/eval/fixtures.py assurance_agent/eval/types.py \
  tests/unit/eval/test_fixtures.py
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations
```

## Preserved

- Current workflow-codegen dataset/suite references and live scorer registration.
- Existing complete L2/L3 tiers remain importable without dynamic review/check seeding.
- Cursor-loop files untouched.

## Suggested Commit (controller)

```text
test(eval): stage truthful codegen-pending fixtures
```
