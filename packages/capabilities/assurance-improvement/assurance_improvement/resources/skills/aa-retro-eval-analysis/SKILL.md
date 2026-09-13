# Retro Eval Analysis

Capability-owned retro-eval-analysis skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Analyze a frozen eval evidence slice. Schema truth is `assurance_improvement.contracts`
for `RetroAnalysisResultV3` with `domain=eval`.

## Inputs

### required

- `change_id` and the authenticated `evidence_slice` supplied in the JSON input
- the slice includes `retro_id`, `domain`, `window`, `entries`, `sources`, integrity, and deterministic signals
- cite only source evidence IDs present in this slice; empty entries are not permission to invent evidence

## Outputs

### required

- `candidates: []`: domain analysis produces signals only
- structured `RetroAnalysisResultV3` with `domain` `eval`
- `analysis_status` `ok` or `failed`
- signals cite only `eval_run_ids` present in the authenticated sources

## Rules

- Treat deterministic slice signals as already included. Analyze entries for additional
  `eval_trend` patterns. Do not copy or re-emit a deterministic signal.
- Aggregate runs by `suite + verdict + failure_signature`. Never emit one signal per run.
- Each `signal_id` may occur only once.
- On failure write `analysis_status: failed`, a non-empty `failure_reason`, and no signals.
- Never write `slice_sha256` or calculate a digest.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/retro/retro-eval-analysis.json`.
- Write and return the same complete `RetroAnalysisResultV3` JSON object. No Markdown fence or prose in the final answer.
- Return the typed result and stop.
