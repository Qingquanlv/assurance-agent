# Retro Workflow Analysis

Capability-owned retro-workflow-analysis skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Analyze a frozen workflow evidence slice. Schema truth is `assurance_improvement.contracts`
for `RetroAnalysisResultV3` with `domain=workflow`.

## Inputs

### required

- `change_id` and the authenticated `evidence_slice` supplied in the JSON input
- the slice includes `retro_id`, `domain`, `window`, `entries`, `sources`, integrity, and deterministic signals
- cite only source evidence IDs present in this slice; empty entries are not permission to invent evidence

## Outputs

### required

- `candidates: []`: domain analysis produces signals only
- structured `RetroAnalysisResultV3` with `domain` `workflow`
- `analysis_status` `ok` or `failed`
- signals cite only `workflow_evidence_ids` present in the authenticated sources

## Rules

- Treat deterministic slice signals as already included. Analyze entries for
  additional patterns. Do not copy or re-emit a deterministic signal.
- Group evidence by typed identity:
  - `gate_verdict`: group by `gate_id + cause`; emit `gate_pushback`
  - `task_failure`: group by `node_id + error_kind + message_fingerprint`
  - `healing_outcome`: group by `operation + outcome`; emit `healing`
  - `skill_drift`: group by `phase + expected_skill`; emit `skill_drift`
- Each `signal_id` may occur only once.
- On failure write `analysis_status: failed`, a non-empty `failure_reason`, and no signals.
- Never write `slice_sha256` or calculate a digest.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/changes/<change-id>/retro/retro-workflow-analysis.json`.
- Write and return the same complete `RetroAnalysisResultV3` JSON object. No Markdown fence or prose in the final answer.
- Return the typed result and stop.
