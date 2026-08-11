---
name: aa-retro-eval-analysis
description: Use when a Retro v3 run requires evaluation-trend analysis from its frozen Eval evidence slice.
---

# Retro Eval Signal Analysis

Read only the multiline agent projection
`qa/retro/<retro-id>/evidence/agent/eval-slice.json`. Write only
`qa/retro/<retro-id>/signals/eval.json`.

The runtime retains the canonical slice separately for digest completion and validation;
do not read or reproduce that canonical slice.

Treat the slice's `deterministic_signals` as runtime-owned signals that are already
included in the assembled context. Analyze `entries` for additional patterns; do not
copy or re-emit a deterministic signal. Each `signal_id` may occur only once in the
output document.

Aggregate runs by `suite + verdict + failure_signature`; never emit one signal per
run. For a repeated failing group, emit one `eval_trend` containing the group count,
all contributing `sample_run_ids`, and the union of `source_change_ids`. Healthy or
isolated runs may produce no signal.

Every signal must cite only `eval_run_ids` present in `sources[].evidence_ids`, keep
the exact suite/verdict/signature, and recommend a test, fixture, prompt, or workflow
change justified by the trend. Do not infer product lifecycle facts.

Write a `SignalDraftDocument` with schema version `3`, matching `retro_id`, domain
`eval`, analyzer `aa-retro-eval-analysis`, and `analysis_status: ok`. On an
irrecoverable analysis failure, write `analysis_status: failed`, a non-empty
`failure_reason`, and no signals.

Never write `slice_sha256`, calculate a digest, read another domain or Retro run, or
modify Eval artifacts. The runtime validates references and inserts the digest.
