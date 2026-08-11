# OpenCode Benchmark Lifecycle Parity Design

## Goal

Bring the OpenCode benchmark driver to lifecycle parity with the verified Cursor
driver without changing OpenCode dispatch, model routing, agent permissions, or
its canonical five-item default batch.

## Scope

The OpenCode driver will gain the following backend-neutral behavior from the
Cursor driver:

1. Pass `max_coverage_repair_attempts` into the full workflow invocation.
2. Snapshot the in-workflow Coverage Repair result before archive can move the
   Change directory.
3. Run the repeatable `metrics-nightly` entrypoint and snapshot its verification
   metrics artifacts before archive.
4. Treat Batch knowledge-proposal promotion as a gate: Retro and benchmark eval
   run only after successful promotion, and promotion failure makes the overall
   benchmark fail.
5. Render Verification Metrics and Coverage Repair sections in the OpenCode run
   summary, and include both in the final benchmark exit decision.

## Preserved OpenCode Behavior

- Workflow tasks continue to use `--adapter opencode`, the configured OpenCode
  server, and phase model routing.
- The script continues to refresh bounded OpenCode agents with
  `aa skill refresh --sync-agents` before checking server readiness.
- OpenCode-specific resume, archive, Retro command construction, log suffixes,
  and run-directory naming remain unchanged.
- The canonical five-item default batch remains unchanged.
- Product test failure continues to produce a completed workflow with
  `final_status=FAIL`; reporting, metrics, and Retro still run, while archive is
  skipped when its existing eligibility gate rejects the Change.

## Implementation Shape

Use the existing backend-neutral functions in `cursor-loop-helpers.sh` for
Coverage Repair snapshots, Verification Metrics summaries, summary row
rendering, and final gates. Keep OpenCode command construction inside
`run-workflow-loop.sh` so no headless `--agent-cmd` assumptions leak into the
OpenCode adapter.

The lifecycle order for each terminal Change is:

1. Complete or resume the full workflow.
2. Record the Coverage Repair result.
3. Run and snapshot `metrics-nightly`.
4. Archive only when the existing workflow/final-status gate permits it.
5. After all Batch members settle, promote knowledge proposals.
6. Run Batch Retro and optional benchmark eval only after promotion succeeds.
7. Render the summary and apply workflow/archive, metrics, Coverage Repair, and
   knowledge-promotion exit gates.

## Error Handling

- Missing or invalid Coverage Repair artifacts produce an
  `invalid_artifacts` row and fail the final evidence gate.
- Verification entrypoint or snapshot failure is recorded in its row and fails
  the final metrics gate.
- Knowledge promotion failure skips Retro/eval, records a typed skipped result,
  and fails the final benchmark gate.
- Metrics and snapshot stages remain best-effort during item processing so all
  terminal Batch members are recorded; strict failure is applied once, after
  the summary is written.

## Verification

Add source-level lifecycle parity tests that fail while the OpenCode script is
missing the new configuration, stage calls, summary sections, and exit gates.
Then run:

- the focused benchmark helper/content tests;
- `bash -n` on the OpenCode, Cursor, and shared helper scripts;
- Ruff checks for changed Python tests;
- a final diff review ensuring OpenCode-specific dispatch and permission setup
  did not change.
