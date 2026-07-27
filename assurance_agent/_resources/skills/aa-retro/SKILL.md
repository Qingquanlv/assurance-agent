---
name: aa-retro
description: Use when a Retro v3 context contains validated actionable signals that require Improvement Candidate proposals.
---

# Retro v3 Improvement Proposer

Read only `qa/retro/<retro-id>/context.json`. Write only:

- `qa/retro/<retro-id>/proposal-candidates.json`
- `qa/retro/<retro-id>/retro-summary.md`

Map validated signals to concrete process Improvements. Do not repeat domain analysis,
read raw evidence, inspect another Retro run, or modify any project artifact.

Each Candidate must use schema version `3`, cite one or more `signal_ids` present in
the context, and retain immutable `source_refs` from those signals. Allowed pairs are:

- `prompt_improvement` → `memory_patch`
- `fixture_improvement` → `memory_patch` or `change_draft`
- `test_improvement` → `memory_patch` or `change_draft`
- `workflow_improvement` → `change_draft`
- `domain_knowledge` → `knowledge_delta`

`domain_knowledge` is allowed only when `context.integrity.status == complete`, must
cite a Problem, and must contain a valid L2 delta. Other Improvement kinds remain
eligible for incomplete analysis when supported by an `ok` domain's signals.

Write `ImprovementCandidateDocumentDraftV3`: include `schema_version`, `retro_id`,
and `candidates`; never write `context_sha256` or calculate a digest. The runtime
inserts the exact context digest before freeze. Every Candidate must include target,
rationale, proposed change, verification, risk, and confidence. Zero Candidates is
valid only when there are no actionable signals; explain that in the summary.

Never add Problem lifecycle fields such as classification, severity, status, version,
root cause, resolution, or disposition.
