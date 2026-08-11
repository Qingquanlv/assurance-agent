# Task 22 Report — Activate Current Scorer, Pending Datasets, Hard Gates

## Status

**DONE** (edit + verify only; no git add/commit — controller owns commits)

Left cursor-loop alone:
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## Files Changed

```
assurance_agent/eval/scorers/codegen.py
assurance_agent/eval/scorers/current_codegen.py
assurance_agent/eval/types.py
assurance_agent/eval/evidence_export.py
assurance_agent/eval/executor.py
benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json
benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml
eval/datasets/workflow-api-codegen/WAC-001.yaml
eval/datasets/workflow-e2e-codegen/WEEC-001.yaml
eval/datasets/workflow-fuzz-codegen/WFUZ-001.yaml
eval/datasets/workflow-performance-codegen/WPER-001.yaml
eval/suites/workflow-api-codegen.yaml
eval/suites/workflow-e2e-codegen.yaml
eval/suites/workflow-fuzz-codegen.yaml
eval/suites/workflow-performance-codegen.yaml
tests/helpers_four_layer_runtime.py
tests/unit/eval/test_scorers.py
tests/unit/eval/test_suite_load_all.py
tests/unit/eval/test_fixtures.py
tests/unit/eval/test_codegen_scorer.py
tests/integration/test_eval_cli.py
.superpowers/sdd/task-22-report.md
```

## What Landed

- Live registration of `current_assurance_chain_rate`, `current_codegen_attempt_rate`, and `selected_test_write_rate` via `bind_and_score_attempt` in `codegen.py`.
- All four workflow-codegen datasets switched to matching `L2-*-codegen-pending` tiers; suites gained exact hard gates (`op: gte`, `value: 1.0`) and regression `{higher_is_better, max_regression: 0.0}`.
- Binder/export fixes so a successful pending-tier run yields all three hard metrics at `1.0`:
  - TreeStore-backed evidence export (sharded objects + snapshot/write-set blobs)
  - Evidence path resolution under `attempt/evidence/...`
  - Planner detection restricted to node `plan` (not `mechanical-plan-checks`)
  - YAML case loading, `project:` logical path stripping, digest/`change:` key normalization
- Atomic-activation tests + real `aa eval run` CLI matrix with `FourLayerDeterministicAdapter` (four pending import samples, no-tier/no-import fresh-root control, fifteen selection policy table, tampered-policy negative).

## Verification

```text
uv run pytest -q \
  tests/unit/eval/test_fixtures.py \
  tests/unit/eval/test_suite_load_all.py \
  tests/unit/eval/test_codegen_scorer.py \
  tests/unit/eval/test_scorers.py \
  tests/integration/test_eval_cli.py -k codegen
→ 66 passed, 57 deselected

uv run ruff check assurance_agent/eval/scorers/codegen.py \
  assurance_agent/eval/types.py \
  assurance_agent/eval/scorers/current_codegen.py \
  assurance_agent/eval/evidence_export.py \
  tests/unit/eval tests/integration/test_eval_cli.py
→ All checks passed

uv run pyright
→ 0 errors
```

Manual probe (all four layers, pending tiers, `FourLayerDeterministicAdapter`):

```text
api / e2e / fuzz / performance →
  current_assurance_chain_rate=1.0
  current_codegen_attempt_rate=1.0
  selected_test_write_rate=1.0
```

## Intentional Deviations

- Skipped Step 6 commit per controller policy.
- Fresh-root control is true no-tier/no-import execute: registry STOP → `inconclusive`/`fail` with hard metrics at 0; pending import path is the hard-pass surface. Executor only `import_checkpoint`s when `fixture_tier` seeds an import path.

## Suggested Commit (controller)

```
feat(eval): activate four-layer evidence gates
```
