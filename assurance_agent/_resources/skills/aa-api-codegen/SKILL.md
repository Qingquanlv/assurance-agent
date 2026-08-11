---
name: aa-api-codegen
description: Generate API tests and a strict generated-files manifest after the API plan gate passes.
---

## Purpose

Generate API tests under `tests/api/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Collection and execution are graph-owned later; do not run pytest collect or claim Traceability Verification evidence.

## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
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
