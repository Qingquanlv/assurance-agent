---
name: aa-e2e-codegen
description: Generate E2E tests and a strict generated-files manifest after the E2E plan gate passes.
---

## Purpose

Generate E2E tests under `tests/e2e/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Do not run pytest collect or claim Traceability Verification evidence.

## Inputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/m4-review-summary.md`
- `change:review/plan-review.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/e2e-codegen-summary.md`
- `change:codegen/e2e-generated-files.json`

### conditional

- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source. Do not run pytest.
