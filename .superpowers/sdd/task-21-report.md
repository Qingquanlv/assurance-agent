# Task 21 Report — Resume, Remediation, Healing, Named Crash-Cut Coverage

## Status

**DONE** (edit + test only; no git add/commit — controller owns commits)

Design §12.1 process-kill / fresh-runtime recovery now converges for packaged
four-layer wrappers: one child, one committed result, no handler rerun after
recorded success. Task 21 Step 8 verify suite is green; Task 20 suite remains green.

## Root cause (blocker unblocked)

After wrapper child-resume, `execute_selected_wave` re-verified
`lease.invocations[0]` (ancestor wave). The ancestor’s `graph:` wrapper was
already `running`, so `preview_selected_wave` returned `None` →
`SelectedWaveDriftError: replan produced no selected wave`.

Secondary seams exposed once that was fixed:

1. Inherited prepared-wave lease was reused for later child supersteps after the
   reserved wave had already committed.
2. Nested task-workspace materialization repair could not capture/apply against
   the mirrored `qa/changes/<id>/` tree, so committed review outputs never
   rematerialized and codegen-precheck STOPped.
3. Fault-worker `target_superstep_committed` matched `_commit_wave` return values
   as task IDs (they are write-set IDs).

## What landed

```
assurance_agent/workflow/graph/scheduler.py   (verify invocation subtree only;
                                               wrapper child-resume flag;
                                               repair restore_change_drift)
assurance_agent/workflow/graph/runtime.py     (clear prepared lease after use;
                                               repair restore_change_drift)
assurance_agent/workflow/graph/models.py      (without_prepared_wave_lease)
assurance_agent/workflow/graph/workspace.py   (task-workspace change mirror;
                                               repair-scoped change restore;
                                               authoritative change: adds on sync apply)
assurance_agent/workflow/graph/leases.py      (graph-wrapper child resume)
assurance_agent/workflow/graph/planner.py     (abandoned wrapper with child not hard-stop)
tests/helpers_four_layer_runtime.py          (crash/resume harness, review scripts)
tests/integration/test_four_layer_resume.py  (ordinary / remediation / healing matrix)
tests/integration/_graph_fault_worker.py     (§12.1 cuts, effect_retry_lock_contended)
tests/integration/test_graph_runtime_faults.py
tests/unit/workflow/graph/test_supersede.py  (pin digest already drifted at tip)
```

Cursor-loop files left untouched.

## Verification

```text
uv run pytest -q tests/integration/test_four_layer_resume.py -k 'review-api'
→ passed

uv run pytest -q \
  tests/integration/test_four_layer_resume.py \
  tests/integration/test_graph_runtime_faults.py \
  tests/integration/test_codegen_fixer_record.py \
  tests/unit/workflow/graph/test_resume_v3.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_supersede.py
→ 163 passed

uv run pytest -q \
  tests/integration/test_four_layer_codegen_only.py \
  tests/integration/test_api_e2e_assurance_flow.py \
  tests/integration/test_fuzz_performance_assurance_flow.py
→ 53 passed (Task 20)

uv run ruff check <Task 21 paths> → clean
uv run pyright → 0 errors
```

## Suggested Commit (controller)

```text
test(runtime): complete assurance recovery matrix
```
