# Retro

Capability-owned retro skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Map validated retro signals to concrete process Improvements. Schema truth is
`assurance_improvement.contracts` for `RetroAnalysisResultV3`.

## Inputs

### required

- locked retro identity and authenticated `RetroSourceManifestV3`
- canonical context digest
- locked signal ids present in the assembled context

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
- Write a draft candidate document: `schema_version`, `retro_id`, and `candidates`.
  Never write `context_sha256` or calculate a digest.
- Zero candidates is valid only when there are no actionable signals.
- Never add Problem lifecycle fields such as classification, severity, status,
  version, root cause, resolution, or disposition.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
