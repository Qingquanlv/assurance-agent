---
name: aa-retro-issue-analysis
description: Use when a Retro v3 run requires issue-domain pattern analysis from its frozen Issue evidence slice.
---

# Retro Issue Signal Analysis

Read only the multiline agent projection
`qa/retro/<retro-id>/evidence/agent/issue-slice.json`. Write only
`qa/retro/<retro-id>/signals/issue.json`.

The runtime retains the canonical slice separately for digest completion and validation;
do not read or reproduce that canonical slice.

Treat the slice's `deterministic_signals` as runtime-owned signals that are already
included in the assembled context. Analyze `entries` for additional patterns; do not
copy or re-emit a deterministic signal. Each `signal_id` may occur only once in the
output document.

Aggregate repeated Occurrences by stable fingerprint, affected surface, and symptom.
Treat `classification_hint` only as supporting context, never as an aggregation key.
Emit `issue_pattern` only when the evidence supports a reusable
process gap: `workflow_gap`, `prompt_gap`, `fixture_gap`, `test_gap`, or
`knowledge_gap`. A product defect by itself is not an Improvement signal.
`surface.kind == workflow` is only a label, not proof of process ownership.
If the recommended fix belongs to the product API, schema, validation, or
authorization implementation, omit the signal; keep the defect in the Issue
ledger instead of turning it into an assurance Improvement.

Never weaken an assert_ideal contract merely because the current product fails
it. Emit a `test_gap` only when the frozen slice independently establishes that
the intended contract is wrong. A `*_product_divergence` symptom alone does not
establish that; omit the signal rather than recommending that tests match the
failing product behavior.

Every signal must:

- cite only `problem_ids`, `occurrence_ids`, or `issue_event_ids` present in
  `sources[].evidence_ids`;
- preserve the slice's surface and symptom rather than inventing a root cause;
- include a stable `signal_id`, concrete `recommended_change`, count, and confidence;
- omit weak one-off observations instead of emitting speculative signals.

Write a `SignalDraftDocument` with schema version `3`, matching `retro_id`, domain
`issue`, analyzer `aa-retro-issue-analysis`, and `analysis_status: ok`. If analysis
cannot be completed, write `analysis_status: failed`, a non-empty `failure_reason`,
and no signals.

Never write `slice_sha256`, calculate a digest, read another domain or Retro run, or
modify Issue state. The runtime validates references and inserts the slice digest.
