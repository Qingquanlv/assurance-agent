# Task 16 Report — Unify Eval Layer Selection and Define Replayable Change Location

## Status

**DONE** (edit + verify only; no git add/commit — controller owns commits)

Tip at start: `14f023d` (briefs @ `a162cb9`). Left cursor-loop alone:
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## Files Changed

```
assurance_agent/eval/selection.py                          (create)
assurance_agent/eval/change_location_evidence.py           (create)
assurance_agent/change_location.py
assurance_agent/eval/runner.py
assurance_agent/eval/executor.py
assurance_agent/eval/types.py
assurance_agent/eval/write_scan.py                         (minimal: accept canonical tuple)
tests/unit/eval/test_selection.py                          (create)
tests/unit/eval/test_change_location_evidence.py           (create)
tests/unit/test_change_location.py
tests/unit/eval/test_runner.py                             (exercised; list-form covered in test_selection)
tests/unit/eval/test_executor.py
tests/integration/test_eval_cli.py
tests/unit/eval/test_write_scan.py                         (caller migration to selected_layers)
tests/unit/eval/test_eval_import_replay.py                 (caller migration)
.superpowers/sdd/task-16-report.md
```

## What Landed

- `normalize_selected_layers(...)` with `MISSING` sentinel and `selection_normalizer/v1`; key-presence semantics (falsey explicit values never default); all 15 subsets + reverse order canonicalize to `LAYER_NAMES` order; default `("api", "e2e")`.
- Runner resolves selection once from suite key presence and passes only `selected_layers` into `execute_attempt`; raw `test_type`/`test_types` kwargs are rejected.
- Executor writes identical canonical tuple into runtime `params.test_types`, `ExecutionResult`, and `execution.json` (`selected_layers` + `selection_normalizer_version` + `runtime_params`).
- Pure change-location seams: `parse_change_roots`, `probe_change_location_candidates` (non-following `lstat`), `decide_change_location`; `resolve_change` delegates to them.
- D17 models `ChangeLocationCandidateV1` / `ChangeLocationEvidenceV1` plus pure `build_change_location_evidence` / `replay_change_location_evidence` over caller-supplied before-manifest leaf facts (no executor persistence yet — Task 17).

## Verification

```text
uv run pytest -q \
  tests/unit/eval/test_selection.py \
  tests/unit/eval/test_change_location_evidence.py \
  tests/unit/test_change_location.py \
  tests/unit/eval/test_runner.py \
  tests/unit/eval/test_executor.py \
  tests/integration/test_eval_cli.py
→ 78 passed

uv run ruff check assurance_agent/eval/selection.py \
  assurance_agent/eval/change_location_evidence.py \
  assurance_agent/change_location.py \
  assurance_agent/eval/runner.py \
  assurance_agent/eval/executor.py tests/unit/eval
→ All checks passed

uv run pyright
→ 0 errors
```

## Intentional Deviations

- Skipped Step 9 commit per controller policy.
- `resolve_write_policy` still builds legacy porcelain allowlists and still requires exactly one layer for `codegen-only`; Task 17 replaces authority with content manifests / `WritePolicyV1` from the selected tuple + active change.
- Overwrote stale unrelated Task 16 report contents previously stored at this path.

## Suggested Commit (controller)

```
feat(eval): normalize layers and define change evidence
```
