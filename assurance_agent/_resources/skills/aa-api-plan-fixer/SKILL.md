---
name: aa-api-plan-fixer
description: Apply safe automatic fixes to API plan artifacts from api-plan-review.json using typed Runtime Context.
---

## Purpose

Apply safe, mechanical API plan fixes authorized by the typed Runtime Context. Never invent endpoints, auth, fixtures, or expected response schema.

## Inputs

### required

- `change:review/api-plan-review.json`
- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/m3-review-summary.md`

### optional

- `change:plans/data-knowledge.proposal.api.yaml`
- `change:proposal.md`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/api-plan-review-apply-summary.md`

### conditional

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/m3-review-summary.md`
- `change:plans/data-knowledge.proposal.api.yaml`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Runtime Context

### required

- `platform:plan-fixer-runtime-context/v1`

## Boundaries

Use only the typed Runtime Context for automatic vs human-approved mode. Do not read host ledgers for mode detection.

Do not write review JSON.

Apply only allowlisted mechanical fixes from the review auto_fix_plan or human-approved reason.
