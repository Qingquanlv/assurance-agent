# Benchmark Eval Metrics Simplification Design

**Date:** 2026-07-27

## 1. Problem

`benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh` currently names its
benchmark-local Eval stage `Eval regression` and composes three separate operations for every
configured suite:

1. `aa eval run` creates a deterministic golden-fixture run and its metrics;
2. `assurance_agent.eval.regression_gate` applies the engine baseline policy;
3. `aa eval compare` prints metric deltas, but its exit status is ignored.

The benchmark does not use this stage to evaluate the current Full Change or a Retro
Improvement Candidate. Its only required use is to collect and display deterministic Eval
metrics. The baseline regression verdict and the extra benchmark exit gate therefore add cost
and imply a proposal-evaluation link that does not exist.

## 2. Decision

Keep this behavior local to the Cursor benchmark script. Do not add a product CLI command and
do not add an Eval node to the Full or Retro graph.

Replace the current regression stage with a benchmark metrics stage:

```text
full Changes + archive + Retro
  -> benchmark Eval suites via `aa eval run`
  -> collect suite verdict/run ID and retain generated metrics artifacts
  -> write the Benchmark Eval Metrics summary
```

The stage must not invoke `assurance_agent.eval.regression_gate`, must not invoke
`aa eval compare`, and must not alter the final benchmark exit code based on Eval verdicts.
Workflow, Archive, and Retro closure remain the authoritative benchmark-result gate.

## 3. Scope

### In scope

- Rename the shell function and report language from Eval regression to Benchmark Eval Metrics.
- Rename configuration to `DO_BENCHMARK_EVAL` and `BENCHMARK_EVAL_SUITES`.
- Preserve a temporary compatibility fallback from `DO_EVAL_REGRESSION` and
  `EVAL_REGRESSION_SUITES`; the new names take precedence.
- Run each configured suite once with `AA_EVAL_FAKE_ADAPTER=1 aa eval run`.
- Record `suite`, absolute Eval `verdict`, and `run_id` in `loop-summary.md`.
- Preserve the generated per-run `metrics.json`, `gate-result.json`, and `report.json` under the
  existing Eval output layout.
- Treat missing `eval/suites` as a skipped metrics stage.
- Represent CLI execution or malformed JSON as an informational `error` row without changing
  the benchmark exit code.

### Out of scope

- Evaluating the current Full Change with a new Eval suite.
- Evaluating `proposal-candidates.json` or an Improvement delivery.
- Changing `aa eval run`, `aa eval compare`, baseline files, or regression policy code.
- Moving Eval output directories or changing Retro's Eval-history reader.
- Adding a benchmark orchestration graph.

## 4. Data and Failure Semantics

The benchmark stage consumes engine-side suite/dataset definitions and uses the SUT as the Eval
run root. Each successful CLI invocation returns `{run_id, verdict}`; the benchmark reports
those values and leaves detailed metrics in the run artifacts.

Eval metric collection is observational. A suite verdict of `fail`, `inconclusive`,
`needs_human_review`, or `error` remains visible but does not override the final result produced
from Full terminality, Archive eligibility, and Retro closure. Infrastructure failure in the
main workflow remains fail-closed through the existing benchmark result gate.

## 5. Testing

The shell behavior must be exercised with a controlled fake `aa` executable rather than source
text assertions. Tests must prove that:

1. configured suites invoke only `aa eval run`;
2. returned verdicts and run IDs are retained as metric rows;
3. a failing/error Eval metric row does not make the benchmark result gate fail;
4. legacy environment names remain a lower-precedence compatibility input;
5. the existing workflow/archive/Retro result gate behavior is unchanged.

## 6. Acceptance Criteria

- No benchmark path invokes `assurance_agent.eval.regression_gate` or `aa eval compare`.
- Cursor benchmark output uses `Benchmark Eval Metrics`, not `Eval regression`.
- The benchmark's final exit code no longer depends on Eval suite verdicts.
- Existing callers using the new environment names work, while legacy names remain accepted
  during migration.
- Focused benchmark helper tests, shell syntax validation, Ruff, and the relevant Python test
  suite pass.
