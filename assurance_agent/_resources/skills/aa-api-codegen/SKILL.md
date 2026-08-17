---
name: aa-api-codegen
description: >-
  Generate API tests and a strict generated-files manifest after the graph's API codegen precondition gate passes. Triggers on: "generate test code from plan", "continue API codegen", "implement api-codegen-plan", "generate /tests/api". Reads Stage 1 plan files and generates pytest code, fixtures, and helpers. Does NOT execute pytest — test execution is Phase 8 aa-run.
---

## Purpose

Generate API tests under `tests/api/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Collection and execution are graph-owned later; do not run pytest collect or claim Traceability Verification evidence.

The graph gate is the progression authority. Reaching this skill means the
`api-codegen-precondition-gate` passed. Do not independently block codegen because
`api-plan-review.json` still records `needs_human_review` or `not_ready`: those facts
remain immutable after an audited `accept_risk`, while the graph ledger carries the
authorized progression decision. Preserve the review facts in the summary and
implement the selected cases.

## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/api-codegen-mapping.yaml`
- `change:plans/m3-review-summary.md`
- `change:review/api-plan-review.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/api/**`
- `repo:tests/api/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`
- `change:codegen/api-generated-files.json`

### conditional

- `repo:tests/api/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest.

Do not modify product source.

Do not run pytest or invent collection evidence.

## Generated-files Manifest Rules

- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping (`plans/api-codegen-mapping.yaml`) for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file whose
  content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target. Never list an unchanged adapter, helper,
  fixture, or shared builder as `reused`; omit unchanged support dependencies
  from the manifest.

When selected API cases or private-root targets exist, an empty `files` array is
invalid. Generate or update every selected codegen-mapping target and list it as a
`test_entry` with the exact mapped Case IDs.
