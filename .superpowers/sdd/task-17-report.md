# Task 17 Report — Content Manifests and Root Evidence Closure

## Status

**DONE** (edit + verify only; no git add/commit — controller owns commits)

Left cursor-loop alone:
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## Files Changed

```
assurance_agent/eval/evidence_export.py                    (create)
assurance_agent/eval/write_scan.py                         (content manifests / WritePolicyV1 / WriteDiffV1)
assurance_agent/eval/change_location_evidence.py           (consumed by executor persistence)
assurance_agent/eval/executor.py                           (D17 + manifests + envelope + export)
assurance_agent/eval/scorers/shared.py                     (fail-closed content integrity)
assurance_agent/workflow/graph/runtime_commit_safety.py    (evidence-export inventory consumers)
tests/unit/eval/test_write_scan.py
tests/unit/eval/test_change_location_evidence.py
tests/unit/eval/test_evidence_export.py                    (create)
tests/unit/eval/test_executor.py
tests/unit/eval/test_eval_import_replay.py                 (WriteDiffV1 + policy scan)
tests/unit/eval/test_scorers.py                            (exercised via shared)
tests/unit/workflow/graph/test_runtime_commit_safety.py    (inventory covered)
.superpowers/sdd/task-17-report.md
```

`workspace.py` already embeds/verifies `base_tree_roots` (D15); export consumes `TreeStore.load_write_set` + `verify_write_set_base_tree_roots` via the commit-safety inventory. No further workspace edit required.

## What Landed

- Strict `WorktreeManifestV1` / `WriteDiffV1` / `WritePolicyV1` with lstat capture (no symlink follow), 250k/4GiB caps, canonical diff replay, and selected-layer current-change policy (no `qa/changes/**` / `eval/out/runs/**` inside attempt SUT).
- Executor: after fixture seeding → before-manifest → D17 config copy + evidence build/replay → policy → runtime → after-manifest/diff → optional root export → strict `ExecutionEvidenceV1` envelope.
- `evidence_export.py`: root event slice (`export_seq` 1..N, gapped `source_seq`), one-pass closure export/replay, manifest verification; porcelain retained diagnostic-only.
- Scorer integrity/forbidden-write fail closed on missing/forged manifests; never trusts legacy count-only `write-diff.json`.
- D14 inventory extended with evidence-export / root-slice / manifest / base_tree_roots consumers.

## Verification

```text
uv run pytest -q \
  tests/unit/eval/test_write_scan.py \
  tests/unit/eval/test_change_location_evidence.py \
  tests/unit/eval/test_evidence_export.py \
  tests/unit/eval/test_executor.py \
  tests/unit/eval/test_scorers.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_task_input_snapshot.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/workflow/graph/test_runtime_commit_safety.py
→ 260 passed

# Pre-commit follow-up (WriteDiffV1 alignment)
uv run pytest -q \
  tests/unit/eval/test_selection.py \
  tests/unit/eval/test_change_location_evidence.py \
  tests/unit/test_change_location.py \
  tests/unit/eval/test_executor.py \
  tests/unit/eval/test_write_scan.py \
  tests/unit/eval/test_evidence_export.py \
  tests/unit/eval/test_eval_import_replay.py \
  tests/integration/test_eval_cli.py \
  tests/unit/workflow/graph/test_runtime_commit_safety.py
→ 162 passed

uv run ruff check assurance_agent/eval/write_scan.py \
  assurance_agent/eval/change_location_evidence.py \
  assurance_agent/eval/evidence_export.py \
  assurance_agent/eval/executor.py \
  assurance_agent/eval/scorers/shared.py tests/unit/eval
→ All checks passed

uv run pyright
→ 0 errors
```

## Follow-up Fix

`test_eval_import_path_forbidden_write_reports_project_diff` still expected legacy `write-diff.json` keys (`violation_paths`, `forbidden_write_executed_count`). Updated to parse `WriteDiffV1` + `WritePolicyV1` and derive violations via `scan_forbidden_writes_from_diff`.

## Intentional Deviations

- Skipped Step 11 commit per controller policy.
- Export object materialization copies referenced CAS/object files when present; historical sparsity preserved by only collecting non-empty object-id fields (not digests). Full v6 object-store planting remains the responsibility of real runtime/fixture paths.
- Stub eval runtimes without a ledger keep `root_invocation_id` but leave export refs null → integrity zero, attempt not infra-failed solely for missing ledger.
- Overwrote stale unrelated Task 17 report contents previously stored at this path.

## Suggested Commit (controller)

```
feat(eval): export content-bound runtime evidence
```
