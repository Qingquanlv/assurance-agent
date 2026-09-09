# Retro

Capability-owned retro skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Map validated retro signals to concrete process Improvements. Schema truth is
`assurance_improvement.contracts` for `RetroAnalysisResultV3`.

## Inputs

### required

- `change_id` and the full locked `context` supplied in the JSON input
- use `context.retro_id`, `context.source_manifest`, integrity, and `context.signals`
- candidate source refs must also be supported by the signals that candidate cites

## Outputs

### required

- structured `RetroAnalysisResultV3`
- every candidate cites one or more locked `signal_ids`
- every source ref is a member of the authenticated retro manifest

## Rules

- Allowed pairs are:
  - `prompt_improvement` → `memory_patch`
  - `fixture_improvement` → `memory_patch` or `change_draft`
  - `test_improvement` → `memory_patch` or `change_draft`
  - `workflow_improvement` → `change_draft`
  - `domain_knowledge` → `knowledge_delta`
- `domain_knowledge` is allowed only when context integrity is complete and must
  cite a Problem plus a valid L2 delta.
- Write the complete `RetroAnalysisResultV3` object: `schema_version`, `retro_id`, `domain: null`,
  `analysis_status`, `failure_reason`, `signals: []`, and `candidates`.
- Synthesis does not create additional signals. Never write `context_sha256` or calculate a digest.
- Zero candidates is valid only when there are no actionable signals.
- Never add Problem lifecycle fields such as classification, severity, status,
  version, root cause, resolution, or disposition.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/changes/<change-id>/retro/retro.json`.
- Write and return the same complete JSON object. No Markdown fence or prose in the final answer.
- Return the typed result and stop.
