---
name: aa-e2e-plan-fixer
description: Apply safe automatic fixes to E2E plan artifacts from plan-review.json using typed Runtime Context.
---

## Purpose

Repair Factory Mapping only on authorized findings from the typed Runtime Context.

Apply safe, mechanical E2E plan fixes authorized by the typed Runtime Context.

## Inputs

### required

- `change:review/plan-review.json`
- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/m4-review-summary.md`

### optional

- `change:plans/data-knowledge.proposal.e2e.yaml`
- `change:proposal.md`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/plan-review-apply-summary.md`

### conditional

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/m4-review-summary.md`
- `change:plans/data-knowledge.proposal.e2e.yaml`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Runtime Context

### required

- `platform:plan-fixer-runtime-context/v1`

## Boundaries

Repair Factory Mapping only on authorized `auto_fix_plan` findings. Do not edit `e2e-plan-checks.json`. Do not edit `.aa/data-knowledge.yaml`. Never promote proposal content into L1.


Use only the typed Runtime Context for automatic vs human-approved mode. Do not read host ledgers for mode detection. Do not write review JSON.
