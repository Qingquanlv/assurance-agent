---
name: aa-retro-workflow-analysis
description: Use when a Retro v3 run requires workflow-domain analysis from its frozen Workflow evidence slice.
---

# Retro Workflow Signal Analysis

Read only `qa/retro/<retro-id>/evidence/workflow-slice.json`. Write only
`qa/retro/<retro-id>/signals/workflow.json`.

Group evidence by its typed identity:

- `gate_verdict`: group by `gate_id + cause`; emit `gate_pushback`.
- `task_failure`: group by `node_id + error_kind + message_fingerprint`; emit
  `task_failure` and distinguish recovered from unresolved patterns.
- `healing_outcome`: group by `operation + outcome`; emit `healing`.
- `skill_drift`: group by `phase + expected_skill`; emit `skill_drift`.

Every signal must cite only `workflow_evidence_ids` present in
`sources[].evidence_ids`, retain the typed grouping fields verbatim, and recommend a
specific process change. Omit weak one-offs; do not reinterpret them as product bugs.

Write a `SignalDraftDocument` with schema version `3`, matching `retro_id`, domain
`workflow`, analyzer `aa-retro-workflow-analysis`, and `analysis_status: ok`. On an
irrecoverable analysis failure, write `analysis_status: failed`, a non-empty
`failure_reason`, and no signals.

Never write `slice_sha256`, calculate a digest, read another domain or Retro run, or
modify workflow artifacts. The runtime validates references and inserts the digest.
