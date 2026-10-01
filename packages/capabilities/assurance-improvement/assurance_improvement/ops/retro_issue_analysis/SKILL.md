# Retro Issue Analysis

Analyze only locked evidence; do not write Ledgers, snapshots, or delivery
instructions, and do not infer state from prior conversation.

Capability-owned retro-issue-analysis skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Analyze a frozen issue evidence slice. Schema truth is `assurance_improvement.contracts`
for `RetroAnalysisResultV3` with `domain=issue`.

## Inputs

### required

- `change_id` and the authenticated `evidence_slice` supplied in the JSON input
- the slice includes `retro_id`, `domain`, `window`, `entries`, `sources`, integrity, and deterministic signals
- cite only source evidence IDs present in this slice; empty entries are not permission to invent evidence

## Outputs

### required

- `candidates: []`: domain analysis produces signals only
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
- Write the typed result to `qa/results/retro/retro-issue-analysis.json`.
- Write and return the same complete `RetroAnalysisResultV3` JSON object. No Markdown fence or prose in the final answer.
- Return the typed result and stop.
