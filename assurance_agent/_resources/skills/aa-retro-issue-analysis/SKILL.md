---
name: aa-retro-issue-analysis
description: Use when a Retro v3 run requires issue-domain pattern analysis from its frozen Issue evidence slice.
---

# Retro Issue Signal Analysis

Read only `qa/retro/<retro-id>/evidence/issue-slice.json`. Write only
`qa/retro/<retro-id>/signals/issue.json`.

Aggregate repeated Occurrences by stable fingerprint, affected surface, and symptom.
Treat `classification_hint` only as supporting context, never as an aggregation key.
Emit `issue_pattern` only when the evidence supports a reusable
process gap: `workflow_gap`, `prompt_gap`, `fixture_gap`, `test_gap`, or
`knowledge_gap`. A product defect by itself is not an Improvement signal.

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
