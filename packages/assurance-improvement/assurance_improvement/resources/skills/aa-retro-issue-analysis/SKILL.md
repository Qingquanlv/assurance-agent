# Retro Issue Analysis

Capability-owned retro-issue-analysis skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Analyze a frozen issue evidence slice. Schema truth is `assurance_improvement.contracts`
for `RetroAnalysisResultV3` with `domain=issue`.

## Inputs

### required

- locked retro identity
- authenticated issue slice digest
- source evidence ids from the issue manifest

## Outputs

### required

- structured `RetroAnalysisResultV3` with `domain` `issue`
- `analysis_status` `ok` or `failed`
- signals cite only `problem_ids`, `occurrence_ids`, or `issue_event_ids` present
  in the authenticated sources

## Rules

- Treat deterministic slice signals as already included. Analyze entries for
  additional `issue_pattern` process gaps: `workflow_gap`, `prompt_gap`,
  `fixture_gap`, `test_gap`, or `knowledge_gap`.
- A product defect by itself is not an Improvement signal.
- Preserve the slice surface and symptom. Do not invent a root cause.
- Each `signal_id` may occur only once.
- On failure write `analysis_status: failed`, a non-empty `failure_reason`, and no signals.
- Never write `slice_sha256` or calculate a digest.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
