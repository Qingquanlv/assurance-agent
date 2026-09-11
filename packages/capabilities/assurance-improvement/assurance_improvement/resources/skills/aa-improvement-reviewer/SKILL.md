# Improvement Reviewer

Capability-owned improvement-reviewer skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Review exactly one frozen Improvement subject. Schema truth is
`assurance_improvement.contracts` for `ImprovementReviewResultV1`.

## Inputs

### required

- locked improvement identity and expected version
- authenticated review subject digest
- source refs already closed against the retro manifest

## Outputs

### required

- structured `ImprovementReviewResultV1`
- `review_type` is `improvement`
- decision is one of `pass`, `changes_requested`, `needs_human_review`, or `reject`

## Rules

- Check evidence traceability, target/scope ownership, verification readiness,
  delivery safety, duplicate or replacement ambiguity, and underestimated risk.
- Omit `review_id`, `improvement_id`, `expected_improvement_version`, and
  `subject_sha256`. Those are runtime-owned bindings.
- You provide analysis only. Never write an Improvement lifecycle state, a
  delivery instruction, or a gate verdict.
- A `reject` decision is advice and does not reject the Improvement.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/review/improvement-review.json`.
- Return the typed result and stop.
